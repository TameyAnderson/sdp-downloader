# -*- coding: utf-8 -*-
"""Sanitising cookies before they are stored.

TikTok puts short-lived technical cookies (msToken, ttwid, s_v_web_id…)
right next to the login ones. They are bound to the browser and the IP that
created them and live for minutes, so they reach the server already dead.
A single one of them makes TikTok answer 403 Forbidden to every request, no
matter how valid the sessionid sitting next to it is, and on top of that it
stops yt-dlp from solving the JS challenge on its own — which is what makes
TikTok work without any cookies at all.

The symptom is treacherous: the file looks right, the domains are there, the
keys are there — and downloading stops working entirely, not just for
age-restricted posts.
"""
import unittest

from helper import load_bot, read

BOT = load_bot()

TAB = "\t"


def row(domain, name, value="value", expires="9999999999"):
    return TAB.join([domain, "TRUE", "/", "TRUE", expires, name, value])


def names_in(text):
    return [l.split(TAB)[5] for l in text.splitlines()
            if not l.startswith("#") and l.count(TAB) >= 6]


FILE = "\n".join([
    "# Netscape HTTP Cookie File",
    row(".instagram.com", "sessionid"),
    row(".instagram.com", "csrftoken"),
    row(".tiktok.com", "sessionid"),
    row(".tiktok.com", "msToken"),
    row(".tiktok.com", "ttwid"),
    row("www.tiktok.com", "s_v_web_id"),
    row(".tiktok.com", "tt_csrf_token"),
    row(".tiktok.com", "odin_tt"),
    row(".tiktok.com", "tt_chain_token"),
])


class TestVolatileCookiesAreDropped(unittest.TestCase):
    def test_every_known_volatile_key_goes(self):
        clean, dropped = BOT.strip_volatile_cookies(FILE)
        kept = names_in(clean)
        for key in ("msToken", "ttwid", "s_v_web_id",
                    "tt_csrf_token", "odin_tt", "tt_chain_token"):
            with self.subTest(key=key):
                self.assertNotIn(key, kept, "%s survived — TikTok will answer 403" % key)
        self.assertEqual(dropped, 6)

    def test_login_keys_survive(self):
        """This is the whole point of the exercise — they must not be touched."""
        kept = names_in(BOT.strip_volatile_cookies(FILE)[0])
        self.assertEqual(kept.count("sessionid"), 2, "a login key was lost")
        self.assertIn("csrftoken", kept)

    def test_other_sites_are_not_touched(self):
        """The same names on someone else's domain are none of our business."""
        text = "\n".join([row(".instagram.com", "ttwid"),
                          row(".example.com", "msToken")])
        clean, dropped = BOT.strip_volatile_cookies(text)
        self.assertEqual(dropped, 0, "another site's session got hurt")
        self.assertEqual(len(names_in(clean)), 2)

    def test_douyin_counts_as_tiktok(self):
        clean, dropped = BOT.strip_volatile_cookies(row(".douyin.com", "msToken"))
        self.assertEqual(dropped, 1)
        self.assertEqual(names_in(clean), [])


class TestFileStaysValid(unittest.TestCase):
    """After sanitising, the file must still be a file yt-dlp can read."""

    def test_comments_and_header_survive(self):
        clean, _ = BOT.strip_volatile_cookies(FILE)
        self.assertTrue(clean.startswith("# Netscape"),
                        "the header was eaten — yt-dlp will not recognise the format")

    def test_ends_with_a_newline(self):
        clean, _ = BOT.strip_volatile_cookies(FILE)
        self.assertTrue(clean.endswith("\n"))
        self.assertFalse(clean.endswith("\n\n"))

    def test_tabs_are_preserved(self):
        """The Netscape format rests on tabs — spaces break it."""
        clean, _ = BOT.strip_volatile_cookies(FILE)
        for line in clean.splitlines():
            if line.startswith("#"):
                continue
            with self.subTest(line=line[:40]):
                self.assertEqual(line.count(TAB), 6)

    def test_result_still_parses(self):
        ok, info = BOT.parse_cookies_txt(BOT.strip_volatile_cookies(FILE)[0])
        self.assertTrue(ok, "the sanitised file stopped passing validation")
        self.assertIn("sessionid", info["auth"])
        self.assertIn("tiktok.com", info["domains"])

    def test_empty_input_is_harmless(self):
        self.assertEqual(BOT.strip_volatile_cookies("")[1], 0)

    def test_clean_file_is_left_alone(self):
        text = "\n".join([row(".instagram.com", "sessionid"),
                          row(".tiktok.com", "sessionid")]) + "\n"
        clean, dropped = BOT.strip_volatile_cookies(text)
        self.assertEqual(dropped, 0)
        self.assertEqual(clean, text)


class TestItIsActuallyWiredIn(unittest.TestCase):
    """A function nobody calls is dead code: the file would be stored dirty."""

    def test_both_upload_paths_use_it(self):
        """The definition plus two intake paths: a file and pasted text."""
        src = read("bot.py")
        self.assertEqual(src.count("strip_volatile_cookies("), 3,
                         "sanitising is not wired into both cookie intake paths")

    def test_nothing_is_stored_unfiltered(self):
        """store_cookies must receive text that is already sanitised."""
        src = read("bot.py")
        for handler in ("async def on_cookies_document", "async def on_cookies_text"):
            block = src[src.index(handler):]
            block = block[:block.index("store_cookies(")]
            with self.subTest(handler=handler):
                self.assertIn("strip_volatile_cookies(", block,
                              "%s: stores the file before sanitising" % handler)

    def test_the_reason_is_written_down(self):
        """The key list looks arbitrary — without a reason someone will "clean" it."""
        src = read("bot.py")
        preamble = src[:src.index("_VOLATILE_COOKIES = ")][-1500:]
        self.assertIn("403", preamble, "does not say why these keys are harmful")


if __name__ == "__main__":
    unittest.main(verbosity=2)
