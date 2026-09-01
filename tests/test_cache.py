# -*- coding: utf-8 -*-
"""The file_id cache: what counts as "the same file".

Two ways this cache lies quietly, both of them invisible from the outside —
the bot answers instantly and confidently with the wrong thing.

Quality: a link fetched once at 480p answered every later request for that
link, including a request for maximum quality.

Origin: a file_id only works on the Telegram server that issued it. Move from
the cloud API to a local Bot API server and every stored id becomes "wrong
file identifier", while the cache itself still looks perfectly healthy.
"""
import json
import os
import unittest

from helper import load_bot


def fresh(**env):
    bot = load_bot(ENABLE_CACHE=1, **env)
    bot.db_init()
    bot.settings_load_sync()
    bot.cache_load()
    return bot


class TestQualityIsPartOfTheKey(unittest.TestCase):
    URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def setUp(self):
        self.bot = fresh()

    def store(self, quality, file_id):
        self.bot._QUALITY.set(quality)
        self.bot.cache_set(self.URL, False, "video", file_id)

    def lookup(self, quality):
        self.bot._QUALITY.set(quality)
        hit = self.bot.cache_get(self.URL, False)
        return hit["file_id"] if hit else None

    def test_a_lower_quality_does_not_answer_for_max(self):
        self.store(480, "id-480")
        self.assertIsNone(self.lookup(None),
                          "480p answered a request with no quality set")
        self.assertIsNone(self.lookup(1080), "480p answered a request for 1080p")

    def test_the_same_quality_still_hits(self):
        self.store(480, "id-480")
        self.assertEqual(self.lookup(480), "id-480", "the cache stopped working")

    def test_each_quality_keeps_its_own_entry(self):
        self.store(480, "id-480")
        self.store(1080, "id-1080")
        self.assertEqual(self.lookup(480), "id-480")
        self.assertEqual(self.lookup(1080), "id-1080")

    def test_audio_bitrate_counts_too(self):
        self.bot._ABR.set(128)
        self.bot.cache_set(self.URL, True, "audio", "mp3-128")
        self.bot._ABR.set(320)
        self.assertIsNone(self.bot.cache_get(self.URL, True),
                          "128 kbps answered a request for 320")

    def test_audio_and_video_never_collide(self):
        self.bot._QUALITY.set(720)
        self.bot._ABR.set(720)
        self.bot.cache_set(self.URL, False, "video", "the-video")
        self.assertIsNone(self.bot.cache_get(self.URL, True),
                          "a video answered a request for audio")


class TestCacheKnowsWhereItCameFrom(unittest.TestCase):
    """file_id values are bound to the server that handed them out."""

    URL = "https://www.tiktok.com/@u/video/7412345678901234567"

    def test_moving_to_a_local_bot_api_drops_the_cache(self):
        cloud = fresh()
        cloud._QUALITY.set(720)
        cloud.cache_set(self.URL, False, "video", "issued-by-the-cloud")
        path = cloud.CACHE_FILE
        self.assertTrue(os.path.exists(path))

        local = load_bot(ENABLE_CACHE=1, CACHE_FILE=path,
                         TELEGRAM_API_URL="http://telegram-bot-api:8081")
        local.db_init()
        local.settings_load_sync()
        local.cache_load()
        local._QUALITY.set(720)
        self.assertIsNone(local.cache_get(self.URL, False),
                          "kept file_id values the new server cannot resolve")

    def test_the_same_api_keeps_the_cache(self):
        first = fresh()
        first._QUALITY.set(720)
        first.cache_set(self.URL, False, "video", "still-valid")

        again = load_bot(ENABLE_CACHE=1, CACHE_FILE=first.CACHE_FILE)
        again.db_init()
        again.settings_load_sync()
        again.cache_load()
        again._QUALITY.set(720)
        hit = again.cache_get(self.URL, False)
        self.assertIsNotNone(hit, "the cache is dropped on every restart")
        self.assertEqual(hit["file_id"], "still-valid")

    def test_an_older_file_is_discarded_not_crashed_on(self):
        """The previous format was a bare {key: value} map."""
        bot = fresh()
        with open(bot.CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"v:" + self.URL: {"kind": "video", "file_id": "old"}}, f)
        bot.cache_load()
        bot._QUALITY.set(720)
        self.assertIsNone(bot.cache_get(self.URL, False))

    def test_garbage_on_disk_is_survivable(self):
        bot = fresh()
        with open(bot.CACHE_FILE, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        bot.cache_load()          # must not raise
        self.assertEqual(bot._cache, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
