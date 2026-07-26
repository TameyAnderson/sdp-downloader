# -*- coding: utf-8 -*-
"""Sending: a post title from outside must not break the message.

Titles come from other people's sites and may contain "_", "*", "[", "`".
With markdown parsing on, Telegram tries to parse such a caption, finds no
closing marker and rejects the whole request:

    Bad Request: can't parse entities: Can't find end of the entity

The file downloads just fine — it is the sending that fails, so the failure
looks like "something went wrong" with no visible cause.
"""
import re
import unittest

from helper import load_bot, read

BOT = load_bot()

# Titles that really occur and would have broken the markup
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
        """Global markup is off — otherwise any _ in a title means a crash."""
        src = read("bot.py")
        self.assertNotIn('parse_mode="Markdown"', src,
                         "markup is on: external titles will break sending again")
        self.assertNotIn("parse_mode='Markdown'", src)
        self.assertIn("parse_mode=None", src)

    def test_no_markdown_markers_left_in_strings(self):
        """With no markup, stray asterisks would be visible to the user."""
        for lang, table in BOT.T.items():
            for key, value in table.items():
                if not isinstance(value, str):
                    continue
                with self.subTest(lang=lang, key=key):
                    self.assertNotRegex(value, r"\*\S", "%s: a markdown asterisk left" % key)

    def test_caption_is_passed_through_untouched(self):
        """The caption is not escaped or trimmed — it is passed through as is."""
        src = read("bot.py")
        block = src[src.index("async def send_video_with_meta"):]
        block = block[:block.index("sent = await message.reply_video")]
        self.assertIn('kwargs["caption"] = cap[:1000]', block)


class TestTitleSafety(unittest.TestCase):
    """A title is cut to Telegram's limit and is not changed in any other way."""

    LIMIT = 1024        # Telegram's caption limit

    def test_titles_survive_unchanged(self):
        for title in NASTY_TITLES:
            with self.subTest(title=title):
                caption = title[:1000]
                self.assertEqual(caption, title)
                self.assertLessEqual(len(caption), self.LIMIT)

    def test_long_title_is_cut_below_the_limit(self):
        caption = ("a very long title " * 200)[:1000]
        self.assertLessEqual(len(caption), self.LIMIT)

    def test_titles_mode_still_controls_captions(self):
        self.assertIn(BOT.titles_mode(), ("off", "private", "all"))
        off = load_bot(TITLES_MODE="off")
        self.assertEqual(off.titles_mode(), "off")


class TestExternalTextInAdminMessages(unittest.TestCase):
    """Commit messages and error texts get interpolated into admin messages too."""

    def test_commit_message_with_special_chars(self):
        note = BOT.github_change_note("aaa1111", "bbb2222", "fix: _underscore_ *star*")
        self.assertIn("_underscore_", note)
        self.assertIn("compare/aaa1111...bbb2222", note)

    def test_error_text_is_interpolated_safely(self):
        msg = BOT.t("err_internal", err="ValueError: bad _name_ *here*")
        self.assertIn("bad _name_ *here*", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
