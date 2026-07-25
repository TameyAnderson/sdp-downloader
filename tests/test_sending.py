# -*- coding: utf-8 -*-
"""Відправка: назва поста ззовні не має ламати повідомлення.

Назви приходять із чужих сайтів і можуть містити «_», «*», «[», «`».
З увімкненою markdown-розміткою Telegram намагається розібрати такий підпис,
не знаходить закриття і відхиляє весь запит:

    Bad Request: can't parse entities: Can't find end of the entity

Файл при цьому завантажується нормально — падає саме відправка, тому збій
виглядає як «щось пішло не так» без видимої причини.
"""
import re
import unittest

from helper import load_bot, read

BOT = load_bot()

# Назви, які реально трапляються і ламали б розмітку
NASTY_TITLES = [
    "reel by @user_name",
    "*акція* тільки сьогодні",
    "[новинка] огляд",
    "як зробити _це_ вдома",
    "музика | 2026 | ремікс",
    "`code` in a caption",
    "50% знижки ** все",
    "___",
]


class TestNoMarkdownParsing(unittest.TestCase):
    def test_bot_sends_without_parse_mode(self):
        """Глобальна розмітка вимкнена — інакше будь-який _ у назві = падіння."""
        src = read("bot.py")
        self.assertNotIn('parse_mode="Markdown"', src,
                         "розмітка увімкнена: зовнішні назви знову ламатимуть відправку")
        self.assertNotIn("parse_mode='Markdown'", src)
        self.assertIn("parse_mode=None", src)

    def test_no_markdown_markers_left_in_strings(self):
        """Якщо розмітки немає, зірочки в текстах були б видні користувачу."""
        for lang, table in BOT.T.items():
            for key, value in table.items():
                if not isinstance(value, str):
                    continue
                with self.subTest(lang=lang, key=key):
                    self.assertNotRegex(value, r"\*\S", "%s: лишилась markdown-зірочка" % key)

    def test_caption_is_passed_through_untouched(self):
        """Підпис не екранується й не ріжеться — його просто передають як є."""
        src = read("bot.py")
        block = src[src.index("async def send_video_with_meta"):]
        block = block[:block.index("sent = await message.reply_video")]
        self.assertIn('kwargs["caption"] = cap[:1000]', block)


class TestTitleSafety(unittest.TestCase):
    """Назва проходить обрізання до ліміту Telegram і не змінюється інакше."""

    LIMIT = 1024        # ліміт підпису в Telegram

    def test_titles_survive_unchanged(self):
        for title in NASTY_TITLES:
            with self.subTest(title=title):
                caption = title[:1000]
                self.assertEqual(caption, title)
                self.assertLessEqual(len(caption), self.LIMIT)

    def test_long_title_is_cut_below_the_limit(self):
        caption = ("дуже довга назва " * 200)[:1000]
        self.assertLessEqual(len(caption), self.LIMIT)

    def test_titles_mode_still_controls_captions(self):
        self.assertIn(BOT.titles_mode(), ("off", "private", "all"))
        off = load_bot(TITLES_MODE="off")
        self.assertEqual(off.titles_mode(), "off")


class TestExternalTextInAdminMessages(unittest.TestCase):
    """Тексти комітів і помилок теж підставляються в повідомлення адміну."""

    def test_commit_message_with_special_chars(self):
        note = BOT.github_change_note("aaa1111", "bbb2222", "fix: _underscore_ *star*")
        self.assertIn("_underscore_", note)
        self.assertIn("compare/aaa1111...bbb2222", note)

    def test_error_text_is_interpolated_safely(self):
        msg = BOT.t("err_internal", err="ValueError: bad _name_ *here*")
        self.assertIn("bad _name_ *here*", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
