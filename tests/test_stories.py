"""Stories require an Instagram session; lower resolutions cannot fix login."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helper import load_bot

STORY = "https://www.instagram.com/stories/example/3983627697225787466/"
REEL = "https://www.instagram.com/reel/example/"


def cookie(domain=".instagram.com", key="sessionid", expiry="9999999999", path="/"):
    return f"{domain}\tTRUE\t{path}\tTRUE\t{expiry}\t{key}\ttest-session"


class TestStoryCookies(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()

    def write(self, row):
        Path(self.bot.COOKIES_FILE).write_text(
            "# Netscape HTTP Cookie File\n" + row + "\n", encoding="utf-8")

    def test_httponly_session_is_accepted_by_upload_and_story_checks(self):
        self.write("#HttpOnly_" + cookie())
        ok, info = self.bot.parse_cookies_txt(Path(self.bot.COOKIES_FILE).read_text())
        self.assertTrue(ok)
        self.assertIn("sessionid", info["auth"])
        self.assertTrue(self.bot.instagram_session_ready())

    def test_missing_file_is_not_a_session(self):
        self.assertFalse(self.bot.instagram_session_ready())

    def test_expired_other_site_csrf_and_wrong_path_do_not_authenticate(self):
        for row in (cookie(expiry="1"), cookie(domain=".tiktok.com"),
                    cookie(domain=".instagram.com.example.org"),
                    cookie(key="csrftoken"), cookie(path="/accounts/")):
            with self.subTest(row=row):
                self.write(row)
                self.assertFalse(self.bot.instagram_session_ready())

    def test_session_cookie_without_expiry_is_accepted(self):
        for expiry in ("", "0"):
            with self.subTest(expiry=expiry):
                self.write(cookie(expiry=expiry))
                self.assertTrue(self.bot.instagram_session_ready())

    def test_malformed_cookie_file_is_rejected(self):
        Path(self.bot.COOKIES_FILE).write_text("not a cookie jar", encoding="utf-8")
        self.assertFalse(self.bot.instagram_session_ready())

    def test_story_detection_uses_host_and_path(self):
        self.assertTrue(self.bot._is_instagram_story(STORY + "?igsh=share"))
        for url in (REEL, "https://example.org/stories/user/123/",
                    "https://example.org/?url=" + STORY):
            self.assertFalse(self.bot._is_instagram_story(url))


class TestStoryRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_bot(BOT_LANG="en")
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type=self.bot.ChatType.PRIVATE), reply=AsyncMock())

    async def route(self, audio=False):
        return await self.bot._do_process(object(), self.message, STORY, True,
                                          audio, "instagram", "instagram")

    async def test_no_session_explains_next_step_without_network(self):
        with patch.object(self.bot, "ytdlp_download", new_callable=AsyncMock) as download:
            self.assertEqual(await self.route(), ("fail", "instagram"))
        download.assert_not_awaited()
        self.message.reply.assert_awaited_once_with(self.bot.t("story_session"))

    async def test_successful_story_uses_ytdlp_even_when_cobalt_is_preferred(self):
        self.bot._settings["engine_pref"] = "cobalt"
        with (patch.object(self.bot, "instagram_session_ready", return_value=True),
              patch.object(self.bot, "ChatActionSender"),
              patch.object(self.bot, "_video_with_cache", new_callable=AsyncMock,
                           return_value="sent") as download,
              patch.object(self.bot, "fetch_title", new_callable=AsyncMock) as title,
              patch.object(self.bot, "handle_cobalt", new_callable=AsyncMock) as cobalt):
            self.assertEqual(await self.route(), ("sent", "instagram"))
        download.assert_awaited_once()
        self.assertTrue(download.call_args.kwargs["allow_silent"])
        title.assert_not_awaited()
        cobalt.assert_not_awaited()

    async def test_access_denied_has_one_download_no_fallback_and_actionable_reply(self):
        async def denied(*args, **kwargs):
            self.bot.note_failure("You need to log in to access this content")
            return None, "denied"

        with (patch.object(self.bot, "instagram_session_ready", return_value=True),
              patch.object(self.bot, "ChatActionSender"),
              patch.object(self.bot, "ytdlp_download", side_effect=denied) as download,
              patch.object(self.bot, "handle_cobalt", new_callable=AsyncMock) as cobalt,
              patch.object(self.bot, "ytdlp_photos", new_callable=AsyncMock) as photos,
              patch.object(self.bot, "engine_result") as breaker):
            self.assertEqual(await self.route(), ("fail", "instagram"))
        download.assert_awaited_once()
        cobalt.assert_not_awaited()
        photos.assert_not_awaited()
        breaker.assert_not_called()
        self.message.reply.assert_awaited_once_with(self.bot.t("story_access"))

    async def test_audio_access_denied_has_story_specific_reply(self):
        async def denied(*args, **kwargs):
            self.bot.note_failure("This content is unreachable")
            return "fail", None

        with (patch.object(self.bot, "instagram_session_ready", return_value=True),
              patch.object(self.bot, "ChatActionSender"),
              patch.object(self.bot, "try_ytdlp_audio", side_effect=denied)):
            self.assertEqual(await self.route(audio=True), ("fail", "instagram"))
        self.message.reply.assert_awaited_once_with(self.bot.t("story_access"))

    async def test_non_access_failure_still_tries_lower_quality(self):
        with patch.object(self.bot, "ytdlp_download", new_callable=AsyncMock,
                          return_value=(None, "network error")) as download:
            status, _ = await self.bot.try_ytdlp_send(
                object(), self.message, STORY, [720, 480, 360], False)
        self.assertEqual(status, "fail")
        self.assertEqual(download.await_count, 3)

    async def test_reel_does_not_require_story_session(self):
        with (patch.object(self.bot, "instagram_session_ready") as session,
              patch.object(self.bot, "ChatActionSender"),
              patch.object(self.bot, "fetch_title", new_callable=AsyncMock),
              patch.object(self.bot, "engine_order", return_value=["ytdlp"]),
              patch.object(self.bot, "_video_with_cache", new_callable=AsyncMock,
                           return_value="sent")):
            result = await self.bot._do_process(object(), self.message, REEL, True,
                                               False, "instagram", "instagram")
        self.assertEqual(result, ("sent", "instagram"))
        session.assert_not_called()
