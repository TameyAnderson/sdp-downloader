"""Telegram attachments: shared local files, cloud downloads, safe failures."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helper import load_bot


class TestTelegramFiles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = load_bot(TELEGRAM_API_URL="http://telegram-bot-api:8081", BOT_LANG="en")
        self.root = Path(self.mod._test_tmp)
        self.mod.TGAPI_ROOTS = (str(self.root),)
        self.bot = SimpleNamespace(get_file=AsyncMock(), download=AsyncMock(),
                                   delete_message=AsyncMock())
        self.doc = SimpleNamespace(file_id="cookie-file", file_name="cookies.txt", file_size=100)
        self.message = SimpleNamespace(document=self.doc, from_user=SimpleNamespace(id=777),
                                       chat=SimpleNamespace(id=777, type=self.mod.ChatType.PRIVATE),
                                       message_id=42, reply=AsyncMock())

    def local_file(self, content=b"cookies"):
        path = self.root / "documents" / "file_147.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        self.bot.get_file.return_value = SimpleNamespace(file_path=str(path))
        return path

    async def test_local_file_is_read_without_http(self):
        self.local_file()
        self.assertEqual(await self.mod.fetch_telegram_file(self.bot, self.doc), b"cookies")
        self.bot.get_file.assert_awaited_once_with("cookie-file")
        self.bot.download.assert_not_awaited()

    async def test_relative_path_from_log_reports_missing_local_mode(self):
        self.bot.get_file.return_value = SimpleNamespace(file_path="documents/file_147.txt")
        with self.assertRaisesRegex(self.mod.TelegramFileFetchError, "TELEGRAM_LOCAL=1"):
            await self.mod.fetch_telegram_file(self.bot, self.doc)
        self.bot.download.assert_not_awaited()

    async def test_missing_absolute_file_reports_mount(self):
        self.bot.get_file.return_value = SimpleNamespace(file_path=str(self.root / "missing.txt"))
        with self.assertRaisesRegex(self.mod.TelegramFileFetchError, "tgapi-data"):
            await self.mod.fetch_telegram_file(self.bot, self.doc)
        self.bot.download.assert_not_awaited()

    async def test_unreadable_local_file_reports_permissions(self):
        path = self.local_file()
        with (patch.object(self.mod, "_find_local_file", return_value=path),
              patch.object(Path, "read_bytes", side_effect=PermissionError("sensitive path")),
              self.assertRaisesRegex(self.mod.TelegramFileFetchError, "read permissions")):
            await self.mod.fetch_telegram_file(self.bot, self.doc)

    async def test_cloud_still_downloads_normally(self):
        self.mod.TELEGRAM_API_URL = ""

        async def download(obj, destination):
            destination.write(b"cloud cookies")

        self.bot.download.side_effect = download
        self.assertEqual(await self.mod.fetch_telegram_file(self.bot, self.doc), b"cloud cookies")
        self.bot.get_file.assert_not_awaited()

    async def test_uploaded_cookies_are_stored_and_message_deleted(self):
        content = ("# Netscape HTTP Cookie File\n"
                   ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\ttest\n")
        self.local_file(content.encode())
        with (patch.object(self.mod, "set_setting_sync"),
              patch.object(self.mod, "settings_load_sync")):
            await self.mod.on_cookies_document(self.message, self.bot)
        self.assertEqual(Path(self.mod.COOKIES_FILE).read_text(encoding="utf-8"), content)
        self.bot.delete_message.assert_awaited_once_with(777, 42)
        self.bot.download.assert_not_awaited()

    async def test_failed_upload_preserves_previous_cookies(self):
        Path(self.mod.COOKIES_FILE).write_text("old cookies", encoding="utf-8")
        self.bot.get_file.return_value = SimpleNamespace(file_path="documents/file_147.txt")
        await self.mod.on_cookies_document(self.message, self.bot)
        self.assertEqual(Path(self.mod.COOKIES_FILE).read_text(), "old cookies")
        self.assertIn("TELEGRAM_LOCAL=1", self.message.reply.call_args.args[0])
        self.bot.delete_message.assert_not_awaited()

    async def test_raw_download_errors_do_not_leak_into_chat_or_logs(self):
        error = RuntimeError(f"404 url=http://telegram-bot-api/file/bot{self.mod.BOT_TOKEN}/doc")
        with (patch.object(self.mod, "fetch_telegram_file", side_effect=error),
              self.assertLogs("video-bot", level="WARNING") as logs):
            await self.mod.on_cookies_document(self.message, self.bot)
        output = self.message.reply.call_args.args[0] + "\n".join(logs.output)
        self.assertNotIn(self.mod.BOT_TOKEN, output)
        self.assertNotIn("http://", output)
        self.assertIn("RuntimeError", output)

    async def test_backup_download_error_is_also_safe(self):
        with patch.object(self.mod, "fetch_telegram_file",
                          side_effect=RuntimeError(f"URL contains {self.mod.BOT_TOKEN}")):
            await self.mod.handle_restore(self.message, self.bot, self.doc)
        self.assertNotIn(self.mod.BOT_TOKEN, self.message.reply.call_args.args[0])

    def test_http_error_preserves_status_only(self):
        error = RuntimeError("secret URL")
        error.status = 404
        self.assertEqual(self.mod.telegram_file_error(error), "Telegram HTTP 404")
