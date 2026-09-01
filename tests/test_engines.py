# -*- coding: utf-8 -*-
"""How a job behaves when things go wrong, and how engines are chosen.

All four of these share a shape: the bot kept working, but it worked stupidly.
It told everyone the same thing whatever happened, it held a slot for half an
hour on a link that was never going to load, it kept trying an engine that had
failed on every link for the last ten minutes, and it downloaded the same video
twice when two people posted it at once.
"""
import asyncio
import unittest

from helper import load_bot, read

BOT = load_bot()

# Real messages, copied from failures seen in production.
FAILURES = [
    ("ERROR: [TikTok] 123: This post may not be comfortable for some audiences. "
     "Log in for access.", "err_age"),
    ("ERROR: [Instagram] xyz: Requested content is not available, rate-limit "
     "reached or login required", "err_private"),
    ("ERROR: [youtube] Video unavailable. This video has been removed by the uploader",
     "err_gone"),
    ("ERROR: The uploader has not made this video available in your country", "err_geo"),
    ("ERROR: [Instagram] Dcju: No video formats found!", "err_extractor"),
    ("ERROR: [TikTok] 76: Unexpected response from webpage request", "err_extractor"),
    ("ERROR: unable to download webpage: HTTP Error 500", "err_download"),
]


class TestFailuresAreNamed(unittest.TestCase):
    def test_known_messages_are_recognised(self):
        for text, expected in FAILURES:
            with self.subTest(text=text[:50]):
                self.assertEqual(BOT.classify_failure(text), expected)

    def test_anything_unknown_stays_generic(self):
        for text in ("", None, "something nobody has seen before"):
            with self.subTest(text=text):
                self.assertEqual(BOT.classify_failure(text), "err_download")

    def test_case_does_not_matter(self):
        self.assertEqual(BOT.classify_failure("VIDEO UNAVAILABLE"), "err_gone")

    def test_every_reason_is_translated(self):
        keys = {k for k, _ in BOT._FAIL_PATTERNS} | {"err_download"}
        for lang in ("uk", "en"):
            for key in keys:
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, BOT.T[lang])

    def test_a_specific_reason_beats_a_generic_one(self):
        """A later generic failure must not overwrite what we already learned."""
        BOT._FAIL_REASON.set(None)
        BOT.note_failure("Video unavailable")
        BOT.note_failure("something odd")
        self.assertEqual(BOT._FAIL_REASON.get(), "err_gone")

    def test_the_reason_reaches_the_user(self):
        src = read("bot.py")
        self.assertIn('t(_FAIL_REASON.get() or "cant_video")', src,
                      "the reason is worked out and then thrown away")

    def test_the_reason_is_reset_per_job(self):
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        block = block[:block.index("async with _user_gate")]
        self.assertIn("_FAIL_REASON.set(None)", block,
                      "the next link would inherit the previous one's reason")


class TestCobaltSession(unittest.TestCase):
    """Cobalt is the fallback for exactly the sites that need a login.

    Cookies went to yt-dlp only, so the fallback was logged out at the moment
    it was supposed to help: yt-dlp fails on a private post, the bot falls back
    to Cobalt, and Cobalt has no session either.
    """

    TAB = "\t"

    def row(self, domain, name, value, prefix=""):
        return prefix + self.TAB.join(
            [domain, "TRUE", "/", "TRUE", "9999999999", name, value])

    def test_cookies_are_grouped_by_service(self):
        text = "\n".join([self.row(".instagram.com", "sessionid", "IG"),
                          self.row(".instagram.com", "ds_user_id", "42"),
                          self.row(".youtube.com", "SID", "YT")])
        jar = BOT.cobalt_cookies_from(text)
        self.assertEqual(set(jar), {"instagram", "youtube"})
        self.assertIn("sessionid=IG", jar["instagram"][0])
        self.assertIn("ds_user_id=42", jar["instagram"][0])
        self.assertEqual(jar["youtube"], ["SID=YT"])

    def test_httponly_lines_are_not_treated_as_comments(self):
        """The prefix looks like a comment and holds the session key."""
        text = self.row(".instagram.com", "sessionid", "IG", "#HttpOnly_")
        self.assertIn("sessionid=IG", BOT.cobalt_cookies_from(text)["instagram"][0])

    def test_real_comments_are_skipped(self):
        text = "# Netscape HTTP Cookie File\n" + self.row(".youtube.com", "SID", "YT")
        self.assertEqual(set(BOT.cobalt_cookies_from(text)), {"youtube"})

    def test_services_cobalt_does_not_read_are_left_out(self):
        text = self.row(".tiktok.com", "sessionid", "TT")
        self.assertEqual(BOT.cobalt_cookies_from(text), {})

    def test_junk_does_not_crash_it(self):
        for junk in ("", "# only a comment", "no tabs here at all"):
            with self.subTest(text=junk):
                self.assertEqual(BOT.cobalt_cookies_from(junk), {})

    def test_empty_values_are_dropped(self):
        text = self.row(".instagram.com", "blank", "")
        self.assertEqual(BOT.cobalt_cookies_from(text), {})

    def test_the_shape_is_what_cobalt_expects(self):
        """service -> list of cookie strings, not a dict and not a bare string."""
        jar = BOT.cobalt_cookies_from(self.row(".instagram.com", "sessionid", "IG"))
        self.assertIsInstance(jar["instagram"], list)
        self.assertEqual(len(jar["instagram"]), 1)
        self.assertIsInstance(jar["instagram"][0], str)

    def test_storing_cookies_feeds_cobalt_too(self):
        src = read("bot.py")
        block = src[src.index("def store_cookies"):]
        block = block[:block.index("\n\n\n")]
        self.assertIn("write_cobalt_cookies(text)", block,
                      "the fallback never sees the session")

    def test_writing_is_opt_in(self):
        """Nothing changes for a deployment that has not configured a path."""
        self.assertEqual(load_bot(COBALT_COOKIES_PATH="").write_cobalt_cookies("x"), 0)


class TestCobaltResponses(unittest.TestCase):
    def test_local_processing_is_asked_to_stay_off(self):
        """We have no code to merge separate streams ourselves."""
        src = read("bot.py")
        block = src[src.index("async def cobalt_request"):]
        block = block[:block.index("async def download_file")]
        self.assertIn('"localProcessing": "disabled"', block)

    def test_it_is_still_handled_if_it_arrives(self):
        """An instance can force it regardless of what we ask for."""
        src = read("bot.py")
        self.assertIn('elif status == "local-processing":', src)
        self.assertIn("FORCE_LOCAL_PROCESSING", src,
                      "the log should name the setting to change")

    def test_unknown_statuses_are_not_swallowed(self):
        src = read("bot.py")
        self.assertIn("Cobalt unsupported status=%s", src)


class TestJobDeadline(unittest.TestCase):
    """A job holds a slot of the per-user gate for its whole life."""

    def test_short_form_gets_the_short_deadline(self):
        self.assertEqual(BOT.job_deadline("https://www.tiktok.com/@u/video/7412345678901234567"),
                         BOT.JOB_DEADLINE_SHORT)

    def test_long_youtube_gets_the_generous_one(self):
        self.assertEqual(BOT.job_deadline("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                         BOT.JOB_DEADLINE_LONG)

    def test_the_long_one_is_actually_longer(self):
        self.assertGreater(BOT.JOB_DEADLINE_LONG, BOT.JOB_DEADLINE_SHORT)

    def test_it_covers_a_single_download(self):
        """yt-dlp caps one full-YouTube subprocess at 1800s — the job must fit it."""
        self.assertGreaterEqual(BOT.JOB_DEADLINE_LONG, 1800)

    def test_the_job_is_actually_wrapped(self):
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        self.assertIn("asyncio.wait_for(", block)
        self.assertIn("timeout=job_deadline(url)", block)

    def test_a_deadline_is_reported_not_swallowed(self):
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        self.assertIn("except asyncio.TimeoutError:", block)
        self.assertIn('t("err_timeout")', block)


class TestCircuitBreaker(unittest.TestCase):
    """When a site changes something, one engine fails on every link of that
    platform while the other still works. Without this the bot spends the whole
    ladder on the broken one first, every single time.
    """

    def setUp(self):
        self.bot = load_bot()
        self.bot._engine_state.clear()

    def test_an_engine_starts_available(self):
        self.assertTrue(self.bot.engine_available("tiktok", "ytdlp"))

    def test_it_trips_after_enough_failures(self):
        for _ in range(self.bot.ENGINE_TRIP_AFTER):
            self.bot.engine_result("tiktok", "ytdlp", False)
        self.assertFalse(self.bot.engine_available("tiktok", "ytdlp"))

    def test_one_failure_short_of_the_limit_keeps_it(self):
        for _ in range(self.bot.ENGINE_TRIP_AFTER - 1):
            self.bot.engine_result("tiktok", "ytdlp", False)
        self.assertTrue(self.bot.engine_available("tiktok", "ytdlp"))

    def test_a_success_clears_the_streak(self):
        """Failures must be consecutive — one good download resets it."""
        for _ in range(self.bot.ENGINE_TRIP_AFTER - 1):
            self.bot.engine_result("tiktok", "ytdlp", False)
        self.bot.engine_result("tiktok", "ytdlp", True)
        self.bot.engine_result("tiktok", "ytdlp", False)
        self.assertTrue(self.bot.engine_available("tiktok", "ytdlp"))

    def test_only_that_pair_is_paused(self):
        for _ in range(self.bot.ENGINE_TRIP_AFTER):
            self.bot.engine_result("tiktok", "ytdlp", False)
        self.assertTrue(self.bot.engine_available("tiktok", "cobalt"),
                        "the other engine was taken down with it")
        self.assertTrue(self.bot.engine_available("instagram", "ytdlp"),
                        "another platform was taken down with it")

    def test_it_can_be_switched_off(self):
        bot = load_bot(ENGINE_TRIP_AFTER=0)
        for _ in range(50):
            bot.engine_result("tiktok", "ytdlp", False)
        self.assertTrue(bot.engine_available("tiktok", "ytdlp"))

    def test_the_loop_honours_it(self):
        src = read("bot.py")
        self.assertIn("if not engine_available(plat, eng):", src)
        self.assertIn("engine_result(plat, eng, True)", src)
        self.assertIn("engine_result(plat, eng, False)", src)


class TestOneDownloadPerLink(unittest.IsolatedAsyncioTestCase):
    """Two people posting the same link at once downloaded it twice."""

    def setUp(self):
        self.bot = load_bot(ENABLE_CACHE=1)
        self.bot.db_init()
        self.bot.settings_load_sync()

    def test_the_same_link_shares_one_lock(self):
        a = self.bot._inflight_lock("v:auto:https://example.com/x")
        b = self.bot._inflight_lock("v:auto:https://example.com/x")
        self.assertIs(a, b)

    def test_different_links_do_not_block_each_other(self):
        a = self.bot._inflight_lock("v:auto:https://example.com/x")
        b = self.bot._inflight_lock("v:auto:https://example.com/y")
        self.assertIsNot(a, b)

    def test_quality_is_part_of_the_identity(self):
        """A 480p job must not wait on a 1080p one — different files."""
        a = self.bot._inflight_lock("v:480:https://example.com/x")
        b = self.bot._inflight_lock("v:1080:https://example.com/x")
        self.assertIsNot(a, b)

    async def test_released_locks_are_swept(self):
        self.bot._inflight_lock("v:auto:https://example.com/gone")
        self.bot.prune_state()
        self.assertEqual(self.bot._inflight, {})

    async def test_a_held_lock_survives_the_sweep(self):
        lock = self.bot._inflight_lock("v:auto:https://example.com/busy")
        async with lock:
            self.bot.prune_state()
            self.assertIn("v:auto:https://example.com/busy", self.bot._inflight)

    def test_the_cache_is_rechecked_after_waiting(self):
        """Otherwise the waiter downloads exactly what it just waited for."""
        src = read("bot.py")
        block = src[src.index("async def _video_with_cache"):]
        block = block[:block.index("\n\n\n")]
        after_lock = block[block.index("async with _inflight_lock("):]
        self.assertIn("from_cache()", after_lock)

    def test_without_a_cache_nobody_waits(self):
        """With nothing to share, waiting only delays a download it still does."""
        src = read("bot.py")
        block = src[src.index("async def _video_with_cache"):]
        block = block[:block.index("\n\n\n")]
        self.assertLess(block.index("if not cache_enabled():"),
                        block.index("async with _inflight_lock("))


if __name__ == "__main__":
    unittest.main(verbosity=2)
