"""All-in accepts registered sites, not every page the generic extractor tries."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from helper import load_bot

YOUTUBE = "https://youtu.be/dQw4w9WgXcQ"
TIKTOK = "https://www.tiktok.com/@user/video/7412345678901234567"
TED = "https://www.ted.com/talks/ken_robinson_says_schools_kill_creativity"
UNSUPPORTED = (
    "https://example.com/page",
    "https://github.com/user/repo",
    "https://uk.wikipedia.org/wiki/Test",
    "https://example.com/video.mp4",
    "https://www.youtube.com.example.org/watch?v=dQw4w9WgXcQ",
    "https://example.com/?next=" + YOUTUBE,
    "https://example.com/path/" + TIKTOK,
    "https://www.youtube.com@evil.example/watch?v=dQw4w9WgXcQ",
)


class TestLinkAllowlist(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot(ENABLE_ALLIN=1)

    def test_unrelated_urls_are_ignored_even_with_allin(self):
        for url in UNSUPPORTED:
            with self.subTest(url=url):
                self.assertEqual(self.bot.extract_urls(url), [])
                self.assertIsNone(self.bot._platform(url))

    def test_additional_sites_come_from_actual_ytdlp_registry(self):
        for url in (TED, "https://streamable.com/abc123"):
            with self.subTest(url=url):
                self.assertEqual(self.bot.extract_urls(url), [url])
                self.assertEqual(self.bot._platform(url), "allin")

    def test_optimised_hidden_service_keeps_its_route(self):
        url = "https://vimeo.com/123456789"
        self.assertEqual(self.bot.extract_urls(url), [url])
        self.assertEqual(self.bot._platform(url), "vimeo")

    def test_primary_services_still_work(self):
        for url in (YOUTUBE, TIKTOK, "https://www.instagram.com/stories/user/123/",
                    "https://fb.watch/abcdef/"):
            with self.subTest(url=url):
                self.assertEqual(self.bot.extract_urls(url), [url])

    def test_disabled_primary_service_does_not_leak_through_allin(self):
        self.bot._settings["svc:tiktok"] = "0"
        self.bot.rebuild_url_pattern()
        self.assertEqual(self.bot.extract_urls(TIKTOK), [])
        self.assertEqual(self.bot.extract_urls(YOUTUBE), [YOUTUBE])

    def test_allin_toggle_takes_effect_without_stale_url_cache(self):
        self.assertEqual(self.bot.extract_urls(TED), [TED])
        self.bot._settings["svc:allin"] = "0"
        self.bot.rebuild_url_pattern()
        self.assertEqual(self.bot.extract_urls(TED), [])
        self.assertEqual(self.bot.extract_urls("https://vimeo.com/123456789"), [])
        self.bot._settings["svc:allin"] = "1"
        self.bot.rebuild_url_pattern()
        self.assertEqual(self.bot.extract_urls(TED), [TED])

    def test_mixed_message_preserves_supported_order_and_deduplicates(self):
        text = f"https://example.com/page {TED} {YOUTUBE} {TED}"
        self.assertEqual(self.bot.extract_urls(text), [TED, YOUTUBE])

    def test_markdown_and_angle_brackets(self):
        self.assertEqual(self.bot.extract_urls(f"[video]({YOUTUBE}), <{TED}>!"), [YOUTUBE, TED])

    def test_generic_and_error_extractors_are_excluded(self):
        self.assertNotIn("Generic", {ie.ie_key() for ie in self.bot._site_extractors()})
        self.assertNotIn("UnsupportedURL", {ie.ie_key() for ie in self.bot._site_extractors()})

    def test_registry_is_not_needed_for_primary_services(self):
        with patch.object(self.bot, "_site_extractors", side_effect=AssertionError("registry")):
            self.assertEqual(self.bot.extract_urls(YOUTUBE), [YOUTUBE])

    def test_repeated_unknown_urls_use_bounded_cache(self):
        matcher = Mock(suitable=Mock(return_value=False))
        with patch.object(self.bot, "_site_extractors", return_value=(matcher,)):
            for _ in range(3):
                self.assertEqual(self.bot.extract_urls("https://example.com/page"), [])
        matcher.suitable.assert_called_once()
        self.assertEqual(self.bot._supported_ytdlp_url.cache_info().maxsize, 2048)

    def test_missing_registry_fails_closed_without_affecting_primary_sites(self):
        with patch.dict("sys.modules", {"yt_dlp.extractor": None}):
            self.assertEqual(self.bot.extract_urls(TED), [])
            self.assertEqual(self.bot.extract_urls(YOUTUBE), [YOUTUBE])


class TestChatSilence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_bot(ENABLE_ALLIN=1)

    async def test_unknown_text_and_caption_do_not_reply_or_start_work(self):
        for caption in (False, True):
            message = SimpleNamespace(text=None if caption else UNSUPPORTED[0],
                                      caption=UNSUPPORTED[0] if caption else None,
                                      reply=AsyncMock())
            with (patch.object(self.bot, "process_url", new_callable=AsyncMock) as download,
                  patch.object(self.bot, "expand_targets", new_callable=AsyncMock) as expand,
                  patch.object(self.bot, "resolve_access") as access):
                await self.bot.handle_message(message, object())
            access.assert_not_called()
            expand.assert_not_awaited()
            download.assert_not_awaited()
            message.reply.assert_not_awaited()

    async def test_mixed_chat_only_downloads_supported_links(self):
        message = SimpleNamespace(text=f"{UNSUPPORTED[0]} {TED} {YOUTUBE}", caption=None,
                                  chat=SimpleNamespace(id=123, type=self.bot.ChatType.SUPERGROUP),
                                  from_user=SimpleNamespace(id=777, username="admin"), reply=AsyncMock())

        async def unchanged(urls, extended):
            return urls

        with (patch.object(self.bot, "process_url", new_callable=AsyncMock) as download,
              patch.object(self.bot, "expand_targets", side_effect=unchanged),
              patch.object(self.bot, "resolve_access", return_value="admin")):
            await self.bot.handle_message(message, object())
        self.assertEqual([call.args[2] for call in download.await_args_list], [TED, YOUTUBE])
