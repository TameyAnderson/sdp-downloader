# -*- coding: utf-8 -*-
"""Link detection: the touchiest spot — TikTok and Instagram broke right here."""
import unittest

from helper import load_bot

BOT = load_bot(ENABLE_TIKTOK=1, ENABLE_YOUTUBE=1, ENABLE_INSTAGRAM=1, ENABLE_FACEBOOK=1)

# (link, expected platform)
CASES = [
    ("https://www.tiktok.com/@user/video/7412345678901234567", "tiktok"),
    ("https://vm.tiktok.com/ZMabcdef/", "tiktok"),
    ("https://www.tiktok.com/@user/photo/7412345678901234567", "tiktok"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/shorts/abc123XYZ_-", "youtube"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://www.instagram.com/reel/Cabcdefghij/", "instagram"),
    ("https://www.instagram.com/p/Cabcdefghij/", "instagram"),
    ("https://www.instagram.com/reels/Cabcdefghij/", "instagram"),
    ("https://www.facebook.com/watch/?v=1234567890", "facebook"),
    ("https://fb.watch/abcdefg/", "facebook"),
]


class TestExtraction(unittest.TestCase):
    def test_finds_link_in_plain_text(self):
        for url, _ in CASES:
            with self.subTest(url=url):
                found = BOT.extract_urls("глянь оце %s топ" % url)
                self.assertTrue(found, "link not found: %s" % url)

    def test_platform_detected(self):
        for url, plat in CASES:
            with self.subTest(url=url):
                self.assertEqual(BOT._platform(url), plat)

    def test_several_links_in_one_message(self):
        text = "%s і ще %s" % (CASES[0][0], CASES[3][0])
        self.assertEqual(len(BOT.extract_urls(text)), 2)

    def test_ignores_non_media_links(self):
        for url in ("https://example.com/page",
                    "https://uk.wikipedia.org/wiki/Тест",
                    "https://github.com/user/repo"):
            with self.subTest(url=url):
                self.assertFalse(BOT.extract_urls("текст " + url))

    def test_no_link_no_reaction(self):
        self.assertFalse(BOT.extract_urls("просто повідомлення без силок"))


class TestInstagramTracking(unittest.TestCase):
    """?igsh=… broke Instagram: Cobalt kept returning error.api.fetch.empty."""

    @staticmethod
    def _download_url(bot, url):
        """What actually reaches the engine (see svc.get("strip") in _do_process)."""
        svc = bot.SERVICES[bot._platform(url)]
        return url.split("?", 1)[0] if svc.get("strip") else url

    def test_instagram_marked_for_stripping(self):
        self.assertTrue(BOT.SERVICES["instagram"].get("strip"),
                        "Instagram needs its query stripped — otherwise the igsh bug is back")

    def test_tracking_params_stripped(self):
        dirty = "https://www.instagram.com/reel/Cabcdefghij/?igsh=MzRlODBiNWFlZA=="
        clean = self._download_url(BOT, dirty)
        self.assertNotIn("igsh", clean)
        self.assertEqual(clean, "https://www.instagram.com/reel/Cabcdefghij/")

    def test_youtube_keeps_its_query(self):
        """On YouTube the id lives in the query itself — stripping kills the link."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertFalse(BOT.SERVICES["youtube"].get("strip"))
        self.assertIn("v=dQw4w9WgXcQ", self._download_url(BOT, url))


class TestKind(unittest.TestCase):
    def test_long_youtube_needs_extended(self):
        self.assertTrue(BOT._needs_extended("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_short_form_is_for_everyone(self):
        for url in ("https://www.tiktok.com/@u/video/7412345678901234567",
                    "https://www.youtube.com/shorts/abc123XYZ_-",
                    "https://www.instagram.com/reel/Cabcdefghij/"):
            with self.subTest(url=url):
                self.assertFalse(BOT._needs_extended(url))

    def test_disabled_service_is_invisible(self):
        off = load_bot(ENABLE_TIKTOK=0, ENABLE_YOUTUBE=1, ENABLE_INSTAGRAM=1)
        self.assertFalse(off.extract_urls("https://www.tiktok.com/@u/video/7412345678901234567"))
        self.assertTrue(off.extract_urls("https://youtu.be/dQw4w9WgXcQ"))

    def test_allin_does_not_shadow_specific_services(self):
        """All-in must come LAST in the registry, or it swallows everything."""
        self.assertEqual(list(BOT.SERVICES)[-1], "allin")
        allin = load_bot(ENABLE_ALLIN=1, ENABLE_TIKTOK=1)
        self.assertEqual(allin._platform("https://www.tiktok.com/@u/video/7412345678901234567"),
                         "tiktok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
