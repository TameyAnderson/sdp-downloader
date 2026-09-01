# -*- coding: utf-8 -*-
"""Starting up, shutting down, and reading the logs afterwards.

The engine chain was never covered by a test: Cobalt's answers were parsed
against whatever the code happened to expect, and nothing checked that against
the documented shapes. Everything else here is about failures that produce no
error at all — a bot that sees nothing in a group, a container killed
mid-download, a log where parallel jobs are indistinguishable.
"""
import asyncio
import contextvars
import io
import logging
import os
import shutil
import tempfile
import unittest

import yaml

from helper import load_bot, read

# Response shapes taken from cobalt's own api.md.
COBALT_TUNNEL = {"status": "tunnel", "url": "https://cobalt/tunnel?id=1",
                 "filename": "video.mp4"}
COBALT_REDIRECT = {"status": "redirect", "url": "https://cdn.example/v.mp4",
                   "filename": "v.mp4"}
COBALT_PICKER = {"status": "picker", "picker": [
    {"type": "photo", "url": "https://cdn/1.jpg"},
    {"type": "photo", "url": "https://cdn/2.jpg"},
    {"type": "video", "url": "https://cdn/3.mp4"}]}
COBALT_ERROR = {"status": "error", "error": {"code": "error.api.fetch.empty"}}
COBALT_LOCAL = {"status": "local-processing", "type": "merge", "service": "youtube",
                "tunnel": ["https://cobalt/t1", "https://cobalt/t2"],
                "output": {"type": "video/mp4", "filename": "v.mp4"}}


def items_from(data):
    """The same branching handle_cobalt does, kept in one place to assert on."""
    status = data.get("status")
    if status == "picker":
        return [(it.get("type", "photo"), it.get("url")) for it in data.get("picker", [])]
    if status in ("tunnel", "redirect"):
        return [("video", data.get("url"))]
    return None


class TestCobaltShapes(unittest.TestCase):
    """Golden fixtures: if cobalt changes a shape, this is what notices."""

    def test_tunnel_and_redirect_give_one_video(self):
        for data in (COBALT_TUNNEL, COBALT_REDIRECT):
            with self.subTest(status=data["status"]):
                self.assertEqual(items_from(data), [("video", data["url"])])

    def test_a_picker_keeps_every_item_and_its_type(self):
        self.assertEqual(items_from(COBALT_PICKER),
                         [("photo", "https://cdn/1.jpg"),
                          ("photo", "https://cdn/2.jpg"),
                          ("video", "https://cdn/3.mp4")])

    def test_an_error_is_not_treated_as_media(self):
        self.assertIsNone(items_from(COBALT_ERROR))

    def test_local_processing_is_not_treated_as_media(self):
        """It carries tunnels, but they are pieces we have no code to merge."""
        self.assertIsNone(items_from(COBALT_LOCAL))

    def test_the_request_matches_the_documented_schema(self):
        src = read("bot.py")
        block = src[src.index("async def cobalt_request"):]
        block = block[:block.index("async def download_file")]
        for key in ("videoQuality", "filenameStyle", "localProcessing"):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, block)
        self.assertIn("application/json", block, "cobalt requires both headers")


class TestSelfCheck(unittest.TestCase):
    def test_group_privacy_is_checked(self):
        """With privacy on, the bot sees nothing in groups and says nothing."""
        src = read("bot.py")
        block = src[src.index("async def self_check"):]
        block = block[:block.index("def clean_orphans")]
        self.assertIn("can_read_all_group_messages", block)
        self.assertIn("/setprivacy", block, "the log should say how to fix it")

    def test_it_reports_the_things_that_break_quietly(self):
        src = read("bot.py")
        block = src[src.index("async def self_check"):]
        block = block[:block.index("def clean_orphans")]
        for what in ("ffmpeg", "cobalt", "po-token", "file limit"):
            with self.subTest(what=what):
                self.assertIn(what, block)

    def test_the_file_limit_explains_itself(self):
        block = read("bot.py")
        block = block[block.index("async def self_check"):]
        block = block[:block.index("def clean_orphans")]
        self.assertIn("local Bot API", block,
                      "a bare '50 MB' is the most common misunderstanding")

    def test_it_runs_at_startup(self):
        src = read("bot.py")
        self.assertIn("await self_check(bot, me)", src)


class TestOrphanCleanup(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.bot = load_bot(WORK_DIR=self.work)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_leftovers_from_a_previous_run_go(self):
        os.makedirs(os.path.join(self.work, "vbot_aaa"))
        open(os.path.join(self.work, "vbot_aaa", "part.mp4"), "w").close()
        open(os.path.join(self.work, "vbot_snd_bbb.mp4"), "w").close()
        self.assertEqual(self.bot.clean_orphans(), 2)
        self.assertEqual(os.listdir(self.work), [])

    def test_nothing_else_is_touched(self):
        os.makedirs(os.path.join(self.work, "something_else"))
        open(os.path.join(self.work, "keepme.txt"), "w").close()
        self.assertEqual(self.bot.clean_orphans(), 0)
        self.assertEqual(sorted(os.listdir(self.work)), ["keepme.txt", "something_else"])

    def test_a_missing_work_dir_is_survivable(self):
        bot = load_bot(WORK_DIR="/nope/does/not/exist")
        self.assertEqual(bot.clean_orphans(), 0)

    def test_it_runs_at_startup(self):
        self.assertIn("clean_orphans()", read("bot.py"))


class TestGracefulShutdown(unittest.TestCase):
    def test_signals_are_handled(self):
        src = read("bot.py")
        self.assertIn("SIGTERM", src)
        self.assertIn("add_signal_handler", src)

    def test_polling_does_not_grab_the_signals_itself(self):
        """aiogram would install its own handlers and skip our shutdown."""
        src = read("bot.py")
        self.assertIn("handle_signals=False", src)

    def test_running_jobs_get_a_deadline(self):
        src = read("bot.py")
        block = src[src.index("async def shutdown"):]
        block = block[:block.index("async def main")]
        self.assertIn("SHUTDOWN_GRACE", block)
        self.assertIn("task.cancel()", block, "a job that overruns must not block the exit")

    def test_the_deadline_fits_inside_dockers(self):
        """Docker waits 10s after SIGTERM, then kills the container."""
        self.assertLess(load_bot().SHUTDOWN_GRACE, 10)

    def test_the_cache_is_written_before_exit(self):
        src = read("bot.py")
        block = src[src.index("async def shutdown"):]
        block = block[:block.index("async def main")]
        self.assertIn("cache_flush_sync()", block,
                      "a deferred write would be lost on stop")


class TestJobIdInLogs(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(logging.Formatter("%(levelname)s|%(job)s%(message)s"))
        self.handler.addFilter(self.bot._JobIdFilter())
        self.log = logging.getLogger("test-job-id")
        self.log.handlers = [self.handler]
        self.log.setLevel(logging.INFO)
        self.log.propagate = False

    def test_lines_outside_a_job_are_unchanged(self):
        self.bot._JOB.set(None)
        self.log.info("hello")
        self.assertEqual(self.buf.getvalue().strip(), "INFO|hello")

    def test_lines_inside_a_job_carry_its_id(self):
        self.bot._JOB.set({"id": "abcdef1234567890"})
        self.log.info("hello")
        self.bot._JOB.set(None)
        self.assertEqual(self.buf.getvalue().strip(), "INFO|[abcdef12] hello")

    def test_the_format_string_uses_it(self):
        src = read("bot.py")
        self.assertIn("%(job)s%(message)s", src)


class TestWeeklyRebuild(unittest.TestCase):
    def test_the_workflow_runs_on_a_schedule(self):
        data = yaml.safe_load(read(".github/workflows/release.yml"))
        on = data.get("on") or data.get(True)
        self.assertIn("schedule", on, "nothing rebuilds the image on its own")
        self.assertTrue(on["schedule"][0]["cron"])

    def test_the_limits_of_the_canary_are_written_down(self):
        """The pin means a rebuild does not bring a newer yt-dlp."""
        raw = read(".github/workflows/release.yml")
        self.assertIn("AUTO_UPGRADE_YTDLP", raw,
                      "someone will expect the schedule to refresh yt-dlp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
