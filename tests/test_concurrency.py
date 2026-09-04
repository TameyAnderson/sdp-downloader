# -*- coding: utf-8 -*-
"""Limits, pacing and background tasks — the things that fail without a trace.

None of these announce themselves. A gate that briefly lets one job too many
through, a retry that ignores the pause it was supposed to keep, a task the
garbage collector quietly reclaims: nothing raises, nothing lands in the log,
the bot simply behaves a little differently than it says it does.
"""
import asyncio
import re
import unittest

from helper import load_bot, read


class TestPerUserGate(unittest.IsolatedAsyncioTestCase):
    """Changing the limit must move the existing gate, not build a second one.

    Rebuilding a Semaphore left the running jobs holding the old one while a
    brand-new, completely free gate stood next to it — for a moment the limit
    was effectively doubled.
    """

    def setUp(self):
        self.bot = load_bot(MAX_PER_USER=1)
        self.bot.db_init()
        self.bot.settings_load_sync()

    def live(self, key, value):
        self.bot.set_setting_sync(key, value)
        self.bot.settings_load_sync()

    def test_the_gate_object_survives_a_limit_change(self):
        gate = self.bot._user_gate(42)
        self.live("max_per_user", 5)
        self.assertIs(self.bot._user_gate(42), gate, "the gate was rebuilt")

    def test_the_ceiling_follows_the_panel(self):
        gate = self.bot._user_gate(42)
        self.assertEqual(gate.limit(), 1)
        self.live("max_per_user", 4)
        self.assertEqual(gate.limit(), 4, "the change did not reach the gate")

    def test_each_user_gets_their_own(self):
        self.assertIsNot(self.bot._user_gate(1), self.bot._user_gate(2))

    async def test_the_limit_actually_holds(self):
        gate = self.bot._user_gate(7)
        inside = []

        async def job(n):
            async with gate:
                inside.append(n)
                await asyncio.sleep(0.05)
                inside.remove(n)

        async def watch():
            peak = 0
            for _ in range(30):
                peak = max(peak, len(inside))
                await asyncio.sleep(0.005)
            return peak

        peak, *_ = await asyncio.gather(watch(), job(1), job(2), job(3))
        self.assertLessEqual(peak, 1, "more jobs ran at once than the limit allows")

    async def test_raising_the_limit_releases_waiters(self):
        gate = self.bot._user_gate(8)
        running = []

        async def job(n):
            async with gate:
                running.append(n)
                await asyncio.sleep(0.15)

        tasks = [asyncio.create_task(job(n)) for n in range(3)]
        await asyncio.sleep(0.02)
        self.assertEqual(len(running), 1)
        self.live("max_per_user", 3)
        # A waiter is woken when a slot is freed, so one release is enough to
        # let the now-higher ceiling take effect.
        await asyncio.gather(*tasks)
        self.assertEqual(len(running), 3)


class TestFloodPacing(unittest.TestCase):
    """The gap must be kept before every attempt, not once per request.

    Keeping it outside the retry loop meant a retry after a 429 went out with
    no gap at all, and the stored timestamp stayed on the attempt Telegram had
    just rejected — so the next message measured its wait from the wrong
    moment and could go out too early as well.
    """

    def block(self):
        src = read("bot.py")
        start = src.index("class FloodMiddleware")
        return src[start:src.index("_notified = {}", start)]

    def test_the_pause_happens_inside_the_retry_loop(self):
        block = self.block()
        loop = block.index("for attempt in range(")
        pause = block.index("await space_out()")
        request = block.index("return await make_request(")
        self.assertLess(loop, pause, "the pause is still outside the loop")
        self.assertLess(pause, request, "the request goes out before the pause")

    def test_the_timestamp_is_written_where_the_pause_is(self):
        block = self.block()
        self.assertIn("_chat_last_send[chat_id] = time.monotonic()", block)
        self.assertEqual(block.count("_chat_last_send[chat_id] ="), 1,
                         "the send time is stamped in more than one place")

    def test_the_interval_is_read_live(self):
        """It is a panel knob — reading it once per process would freeze it."""
        self.assertIn('tunable("chat_interval")', self.block())


class TestBackgroundTasks(unittest.IsolatedAsyncioTestCase):
    """asyncio holds only a weak reference to a running task.

    A task nobody else keeps can be collected mid-flight and simply stop, with
    no error raised anywhere — which is why every one of them is spawned
    through a helper that holds on to it.
    """

    def setUp(self):
        self.bot = load_bot()

    async def test_a_reference_is_held_while_it_runs(self):
        async def slow():
            await asyncio.sleep(0.05)

        task = self.bot.spawn(slow())
        self.assertIn(task, self.bot._background, "nobody is holding the task")
        await task

    async def test_the_reference_is_released_afterwards(self):
        async def quick():
            return None

        task = self.bot.spawn(quick())
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, self.bot._background, "finished tasks pile up forever")

    def test_nothing_starts_a_task_behind_the_helper(self):
        src = read("bot.py")
        stray = [l.strip() for l in src.splitlines()
                 if "asyncio.create_task(" in l and "def spawn" not in l
                 and not l.strip().startswith("#")]
        # the only allowed one is inside spawn() itself
        self.assertEqual(len(stray), 1, "create_task used directly: %s" % stray)
        self.assertIn("task = asyncio.create_task(coro)", stray[0])


class TestBookkeepingIsPruned(unittest.IsolatedAsyncioTestCase):
    """Every one of these dicts is keyed by a chat or a user, and nothing ever
    removed an entry: a bot in a busy group collected one lock, one timestamp
    and one gate per participant and kept them until restart.
    """

    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()
        self.bot.settings_load_sync()

    def test_stale_chat_state_goes(self):
        old = -self.bot.STATE_TTL - 1        # monotonic() starts near zero
        self.bot._chat_last_send[-100] = old
        self.bot._chat_send_lock[-100] = asyncio.Lock()
        self.bot.prune_state()
        self.assertNotIn(-100, self.bot._chat_last_send)
        self.assertNotIn(-100, self.bot._chat_send_lock)

    def test_recent_chat_state_stays(self):
        import time as _t
        self.bot._chat_last_send[-200] = _t.monotonic()
        self.bot._chat_send_lock[-200] = asyncio.Lock()
        self.bot.prune_state()
        self.assertIn(-200, self.bot._chat_last_send)

    async def test_a_chat_mid_send_is_left_alone(self):
        """Dropping the lock under a send in flight would break the pacing."""
        lock = asyncio.Lock()
        self.bot._chat_last_send[-300] = -self.bot.STATE_TTL - 1
        self.bot._chat_send_lock[-300] = lock
        async with lock:
            self.bot.prune_state()
            self.assertIn(-300, self.bot._chat_send_lock)

    def test_idle_user_gates_go(self):
        self.bot._user_gate(555)
        self.assertIn(555, self.bot._user_sems)
        self.bot.prune_state()
        self.assertNotIn(555, self.bot._user_sems)

    async def test_a_gate_in_use_is_kept(self):
        """Dropping it would hand the next job a second, empty gate."""
        gate = self.bot._user_gate(556)
        async with gate:
            self.bot.prune_state()
            self.assertIs(self.bot._user_sems.get(556), gate)

    def test_expired_rate_limit_buckets_go(self):
        self.bot._api_hits["someone"] = [-self.bot.API_RATE_WINDOW - 1]
        self.bot.prune_state()
        self.assertNotIn("someone", self.bot._api_hits)

    def test_the_sweep_is_actually_scheduled(self):
        src = read("bot.py")
        self.assertRegex(src, r"spawn\(\s*housekeeping_loop\(",
                      "the sweep exists but nobody runs it")


class TestCacheWritesOffTheLoop(unittest.IsolatedAsyncioTestCase):
    """json.dump of the whole cache ran inline on every stored file_id."""

    def setUp(self):
        self.bot = load_bot(ENABLE_CACHE=1)
        self.bot.db_init()
        self.bot.settings_load_sync()
        self.bot.cache_load()

    async def test_a_write_is_deferred_while_the_loop_runs(self):
        self.bot._QUALITY.set(720)
        self.bot.cache_set("https://example.com/a", False, "video", "id-a")
        self.assertTrue(self.bot._cache_dirty, "it wrote inline again")
        await self.bot.cache_flush()
        self.assertFalse(self.bot._cache_dirty)

    def test_without_a_loop_it_writes_immediately(self):
        """Tests and shutdown have nothing to block — behaviour stays simple."""
        import os
        self.bot._QUALITY.set(720)
        self.bot.cache_set("https://example.com/b", False, "video", "id-b")
        self.assertFalse(self.bot._cache_dirty)
        self.assertTrue(os.path.exists(self.bot.CACHE_FILE))

    def test_the_flush_is_part_of_housekeeping(self):
        src = read("bot.py")
        block = src[src.index("async def housekeeping_loop"):]
        block = block[:block.index("async def cache_cleaner_loop")]
        self.assertIn("await cache_flush()", block,
                      "a deferred write that nobody flushes is a lost write")


class TestWorkDirCapacity(unittest.TestCase):
    """/tmp is a 1 GB tmpfs in both stacks, while MAX_FILE_MB reaches 2000."""

    def test_the_work_dir_is_configurable(self):
        bot = load_bot(WORK_DIR="/var/tmp")
        self.assertEqual(bot.WORK_DIR, "/var/tmp")

    def test_it_defaults_to_the_system_temp(self):
        import tempfile
        self.assertEqual(load_bot().WORK_DIR, tempfile.gettempdir())

    def test_downloads_are_assembled_there(self):
        src = read("bot.py")
        self.assertNotIn('Path(tempfile.gettempdir()) / ("vbot_', src,
                         "a download path still ignores WORK_DIR")

    def test_a_job_is_refused_when_the_disk_is_full(self):
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        block = block[:block.index("async with _user_gate")]
        self.assertIn("MIN_FREE_SPACE", block)
        self.assertIn('t("no_space")', block)

    def test_the_full_stack_does_not_assemble_downloads_in_ram(self):
        """/tmp is a 1 GB tmpfs; a 4K download needs about twice its own size.

        Leaving it there is how a video announced as 4K arrived as 1080p.
        """
        import yaml
        env = yaml.safe_load(read("docker-compose.yml"))["services"]["video-bot"]["environment"]
        self.assertIn("WORK_DIR", env, "downloads still land in the tmpfs")
        self.assertIn("/data/", env["WORK_DIR"], "the work dir is not on the volume")

    def test_lite_keeps_the_default(self):
        """LITE has no volumes at all — /tmp is the only place it has."""
        import yaml
        env = yaml.safe_load(read("docker-compose.lite.yml"))["services"]["video-bot"]["environment"]
        self.assertNotIn("WORK_DIR", env)

    def test_the_mismatch_is_reported_at_startup(self):
        src = read("bot.py")
        self.assertIn("check_workdir_capacity()", src)
        block = src[src.index("def check_workdir_capacity"):]
        block = block[:block.index("async def housekeeping_loop")]
        self.assertIn("MAX_FILE_SIZE * 2", block,
                      "merging needs the video, the audio and the result at once")


class TestJobStateIsolation(unittest.TestCase):
    """Per-job state lives in contextvars, which are copied per task.

    That is the whole isolation mechanism: two links in one message keep their
    own quality, trim and title only because each one runs in its own task.
    Awaiting process_url directly in a shared task would make the second link
    inherit whatever the first one left behind — no error, just a wrong caption
    or a wrong quality.
    """

    def test_every_call_site_starts_its_own_task(self):
        src = read("bot.py")
        for i, line in enumerate(src.splitlines()):
            if "process_url(" not in line or line.lstrip().startswith("#"):
                continue
            if "async def process_url" in line:
                continue
            context = "\n".join(src.splitlines()[max(0, i - 2):i + 1])
            with self.subTest(line=line.strip()[:60]):
                self.assertTrue("spawn(" in context or "gather(" in context,
                                "process_url is awaited in a shared context")

    def test_the_job_state_is_set_at_the_top_of_the_job(self):
        """Setting any of these before the task exists would leak them."""
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        block = block[:block.index("async with _user_gate")]
        for var in ("_JOB.set(", "_TRIM.set(", "_QUALITY.set(", "_ABR.set("):
            with self.subTest(var=var):
                self.assertIn(var, block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
