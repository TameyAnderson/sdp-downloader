# -*- coding: utf-8 -*-
"""Live settings: a value from the panel must take effect at once and stay in range."""
import unittest

from helper import load_bot, read


class TestTunables(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()
        self.bot.settings_load_sync()

    def live(self, key, value):
        self.bot.set_setting_sync(key, value)
        self.bot.settings_load_sync()

    def test_every_knob_is_clamped(self):
        for key, (env_default, _cast, lo, hi) in self.bot.TUNABLES.items():
            with self.subTest(key=key):
                self.live(key, hi + 1000)
                self.assertEqual(self.bot.tunable(key), hi, "%s: ceiling does not hold" % key)
                self.live(key, lo - 1000)
                self.assertEqual(self.bot.tunable(key), lo, "%s: floor does not hold" % key)

    def test_garbage_falls_back_to_env(self):
        for key, (env_default, _c, _lo, _hi) in self.bot.TUNABLES.items():
            with self.subTest(key=key):
                self.live(key, "not a number")
                self.assertEqual(self.bot.tunable(key), env_default())

    def test_flags_toggle(self):
        for key in self.bot.FLAGS:
            with self.subTest(key=key):
                self.live(key, "1")
                self.assertTrue(self.bot.flag(key))
                self.live(key, "0")
                self.assertFalse(self.bot.flag(key))

    def test_clean_db_uses_env_defaults(self):
        bot = load_bot(MAX_HEIGHT=1080, MAX_CONCURRENT_DOWNLOADS=4, VERIFY_MEDIA=0)
        bot.db_init()
        bot.settings_load_sync()
        self.assertEqual(bot.tunable("max_height"), 1080)
        self.assertEqual(bot.tunable("max_concurrent"), 4)
        self.assertFalse(bot.flag("verify_media"))


class TestLadders(unittest.TestCase):
    """Ladders are computed per job — the ceiling changes without a restart."""

    def setUp(self):
        self.bot = load_bot(MAX_HEIGHT=720, LONG_MAX_HEIGHT=2160, LONG_MIN_HEIGHT=720)
        self.bot.db_init()
        self.bot.settings_load_sync()

    def live(self, key, value):
        self.bot.set_setting_sync(key, value)
        self.bot.settings_load_sync()

    def test_default_from_env(self):
        self.assertEqual(self.bot.quality_ladder(), [720, 480, 360])
        self.assertEqual(self.bot.long_ladder(), [2160, 1440, 1080, 720])

    def test_changes_take_effect(self):
        self.live("max_height", 1080)
        self.assertEqual(self.bot.quality_ladder()[0], 1080)
        self.live("long_max_height", 4320)
        self.assertEqual(self.bot.long_ladder()[0], 4320)

    def test_never_empty(self):
        for hi in (240, 720, 1080, 4320):
            for lo in (144, 720, 1080, 4320):
                with self.subTest(hi=hi, lo=lo):
                    self.live("max_height", hi)
                    self.live("long_max_height", hi)
                    self.live("long_min_height", lo)
                    self.assertTrue(self.bot.quality_ladder())
                    self.assertTrue(self.bot.long_ladder(),
                                    "min>max must not produce an empty ladder")

    def test_ladder_descends(self):
        self.assertEqual(self.bot.long_ladder(), sorted(self.bot.long_ladder(), reverse=True))


class TestYtdlpArgs(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot(CONCURRENT_FRAGMENTS=5, YTDLP_SLEEP=0)
        self.bot.db_init()
        self.bot.settings_load_sync()

    def test_fragments_and_sleep_are_live(self):
        args = " ".join(self.bot.common_ytdlp())
        self.assertIn("--concurrent-fragments", args)
        self.assertNotIn("--sleep-requests", args)

        self.bot.set_setting_sync("frag_concurrency", 1)
        self.bot.set_setting_sync("ytdlp_sleep", "1")
        self.bot.settings_load_sync()
        args = self.bot.common_ytdlp()
        self.assertNotIn("--concurrent-fragments", args)
        self.assertIn("--sleep-requests", args)


class TestTikTokGoesThroughTheApi(unittest.TestCase):
    """TikTok serves a logged-in session a different page layout than yt-dlp's
    parser expects, and extraction dies on "Unable to extract universal data
    for rehydration". Going to the mobile API skips the page entirely.
    """

    TIKTOK = "https://www.tiktok.com/@someone/video/7665817957959896340"

    def setUp(self):
        self.bot = load_bot()

    def args_for(self, url):
        return " ".join(self.bot._with_auth([], url))

    def test_tiktok_links_get_the_api_host(self):
        args = self.args_for(self.TIKTOK)
        self.assertIn("--extractor-args", args)
        self.assertIn("tiktok:api_hostname=", args)

    def test_short_links_count_too(self):
        for short in ("https://vm.tiktok.com/ZMabc/", "https://vt.tiktok.com/ZSxyz/"):
            with self.subTest(url=short):
                self.assertIn("tiktok:api_hostname=", self.args_for(short))

    def test_other_platforms_are_untouched(self):
        for url in ("https://www.instagram.com/reel/Abc123/",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
            with self.subTest(url=url):
                self.assertNotIn("--extractor-args", self.args_for(url))

    def test_it_can_be_turned_off(self):
        """If TikTok ever kills this host, there must be a knob, not a code edit."""
        off = load_bot(TIKTOK_API_HOSTNAME="")
        self.assertNotIn("--extractor-args", " ".join(off._with_auth([], self.TIKTOK)))

    def test_every_call_site_passes_the_url(self):
        """A forgotten url = a silent fall back to the broken page parsing."""
        src = read("bot.py")
        self.assertNotIn("_with_auth(args)", src,
                         "called without the link somewhere — TikTok goes back to HTML")
        self.assertEqual(src.count("_with_auth(args, url)"), 5)


class TestAccess(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()
        self.bot.settings_load_sync()

    def test_whitelist_off_means_open(self):
        self.assertEqual(self.bot.resolve_access(5, "u", 5, False), "extended")
        self.assertEqual(self.bot.resolve_access(5, "u", -100, True), "extended")

    def test_whitelist_on_limits_to_listed_chats(self):
        self.bot.access_add_sync("chat", "-100123", "full", "Own chat")
        self.bot.set_setting_sync("whitelist", "1")
        self.bot.settings_load_sync()
        self.assertEqual(self.bot.resolve_access(5, "u", -100123, True), "extended")
        self.assertEqual(self.bot.resolve_access(5, "u", -100999, True), "none")
        self.assertEqual(self.bot.resolve_access(5, "u", 5, False), "none",
                         "private chats with the whitelist on — admin only")
        self.assertEqual(self.bot.resolve_access(777, "u", 777, False), "admin")

    def test_migration_from_old_model(self):
        """An old database: "listed only" mode, plus users and access levels."""
        import sqlite3
        bot = load_bot()
        con = sqlite3.connect(bot.STATS_DB)
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE access(kind TEXT, ident TEXT, level TEXT, label TEXT,"
                    " PRIMARY KEY(kind, ident))")
        con.execute("INSERT INTO settings VALUES('access_mode','restricted')")
        con.executemany("INSERT INTO access VALUES(?,?,?,?)", [
            ("user", "@someone", "basic", "Someone"),
            ("chat", "-100123", "basic", "Chat")])
        con.commit()
        con.close()

        bot.db_init()
        bot.settings_load_sync()
        self.assertEqual(bot.setting("whitelist"), "1",
                         "the mode should have turned into a toggle")
        self.assertEqual([r["ident"] for r in bot.access_list_sync()], ["-100123"])
        self.assertNotIn(("user", "@someone"), bot._access,
                         "user entries should be gone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
