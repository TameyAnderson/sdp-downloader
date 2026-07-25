# -*- coding: utf-8 -*-
"""LITE: тільки групи, нічого на диску, жодних важких можливостей."""
import os
import unittest

from helper import load_bot


class TestLiteState(unittest.TestCase):
    def setUp(self):
        # Навмисно вмикаємо все, що LITE має придушити.
        self.bot = load_bot(LITE=1, ENABLE_CACHE=1, WEBAPP_ENABLED=1)
        self.bot.db_init()
        self.bot.settings_load_sync()
        self.bot.cache_load()

    def test_nothing_written_to_disk(self):
        self.bot.set_setting_sync("max_height", 1080)
        self.bot._db_record_sync((1,) * 12)
        self.bot.settings_load_sync()
        self.assertFalse(os.path.exists(self.bot.STATS_DB), "LITE не має створювати базу")
        self.assertFalse(os.path.exists(self.bot.CACHE_FILE), "LITE не має створювати кеш")

    def test_cache_forced_off(self):
        self.assertFalse(self.bot.cache_enabled())

    def test_miniapp_forced_off(self):
        self.assertFalse(self.bot.WEBAPP_ENABLED, "Mini App у LITE не піднімається ніколи")

    def test_settings_cannot_be_saved(self):
        self.bot.set_setting_sync("max_height", 4320)
        self.bot.settings_load_sync()
        self.assertEqual(self.bot.tunable("max_height"), self.bot.MAX_HEIGHT)


class TestLiteAccess(unittest.TestCase):
    def test_groups_only(self):
        bot = load_bot(LITE=1)
        self.assertEqual(bot.resolve_access(5, "u", -100123, True), "lite")
        self.assertEqual(bot.resolve_access(5, "u", 5, False), "none")
        self.assertEqual(bot.resolve_access(777, "u", 777, False), "none",
                         "у LITE навіть адмін не качає в приваті")

    def test_allowed_chats(self):
        bot = load_bot(LITE=1, ALLOWED_CHATS="-100123, -100456")
        self.assertEqual(bot.ALLOWED_CHATS, {"-100123", "-100456"})
        self.assertEqual(bot.resolve_access(5, "u", -100123, True), "lite")
        self.assertEqual(bot.resolve_access(5, "u", -100999, True), "none")

    def test_empty_list_means_any_group(self):
        bot = load_bot(LITE=1, ALLOWED_CHATS="")
        self.assertEqual(bot.resolve_access(5, "u", -100999, True), "lite")

    def test_lite_level_is_not_extended(self):
        """Саме звідси випливає: без довгого YouTube, mp3, плейлистів і обрізки."""
        bot = load_bot(LITE=1)
        level = bot.resolve_access(5, "u", -100123, True)
        self.assertNotIn(level, ("extended", "admin"))


class TestLiteEnvSwitches(unittest.TestCase):
    def test_audio_too_from_env(self):
        self.assertFalse(load_bot(LITE=1).audio_too_enabled())
        self.assertTrue(load_bot(LITE=1, AUDIO_TOO=1).audio_too_enabled())

    def test_titles_mode_from_env(self):
        self.assertEqual(load_bot(LITE=1).titles_mode(), "private")
        self.assertEqual(load_bot(LITE=1, TITLES_MODE="off").titles_mode(), "off")
        self.assertEqual(load_bot(LITE=1, TITLES_MODE="дурня").titles_mode(), "private")

    def test_language_from_env(self):
        self.assertEqual(load_bot(LITE=1, BOT_LANG="en").cur_lang(), "en")
        self.assertEqual(load_bot(LITE=1, BOT_LANG="кхм").cur_lang(), "uk")


class TestFullNotAffected(unittest.TestCase):
    """Найважливіше: LITE не має нічого зламати у звичайному режимі."""

    def test_full_still_writes_and_serves(self):
        bot = load_bot(WEBAPP_ENABLED=1)
        bot.db_init()
        bot.settings_load_sync()
        self.assertTrue(os.path.exists(bot.STATS_DB))
        self.assertTrue(bot.WEBAPP_ENABLED)
        self.assertEqual(bot.resolve_access(5, "u", 5, False), "extended")


if __name__ == "__main__":
    unittest.main(verbosity=2)
