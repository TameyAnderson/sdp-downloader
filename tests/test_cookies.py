# -*- coding: utf-8 -*-
"""Чистка cookies перед збереженням.

TikTok кладе поряд із логін-ключами короткоживучі технічні cookies
(msToken, ttwid, s_v_web_id…). Вони прив'язані до браузера та IP, де
створені, і живуть хвилини — на сервер приїжджають уже мертвими.
Один такий ключ змушує TikTok відповідати 403 Forbidden на будь-який
запит, хоч би який валідний sessionid лежав поруч, і додатково не дає
yt-dlp самому розв'язати JS-виклик — а саме на цьому тримається
завантаження з TikTok узагалі без cookies.

Симптом підступний: файл виглядає правильним, домени на місці, ключі
на місці — а качати перестає геть усе, не лише вікові пости.
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
                self.assertNotIn(key, kept, "%s лишився — TikTok відповість 403" % key)
        self.assertEqual(dropped, 6)

    def test_login_keys_survive(self):
        """Заради них усе й робиться — їх чіпати не можна."""
        kept = names_in(BOT.strip_volatile_cookies(FILE)[0])
        self.assertEqual(kept.count("sessionid"), 2, "втрачено логін-ключ")
        self.assertIn("csrftoken", kept)

    def test_other_sites_are_not_touched(self):
        """Ті самі назви на чужому домені — не наша справа."""
        text = "\n".join([row(".instagram.com", "ttwid"),
                          row(".example.com", "msToken")])
        clean, dropped = BOT.strip_volatile_cookies(text)
        self.assertEqual(dropped, 0, "постраждала сесія іншого сайту")
        self.assertEqual(len(names_in(clean)), 2)

    def test_douyin_counts_as_tiktok(self):
        clean, dropped = BOT.strip_volatile_cookies(row(".douyin.com", "msToken"))
        self.assertEqual(dropped, 1)
        self.assertEqual(names_in(clean), [])


class TestFileStaysValid(unittest.TestCase):
    """Після чистки файл має лишитись файлом, який читає yt-dlp."""

    def test_comments_and_header_survive(self):
        clean, _ = BOT.strip_volatile_cookies(FILE)
        self.assertTrue(clean.startswith("# Netscape"),
                        "заголовок з'їли — yt-dlp не впізнає формат")

    def test_ends_with_a_newline(self):
        clean, _ = BOT.strip_volatile_cookies(FILE)
        self.assertTrue(clean.endswith("\n"))
        self.assertFalse(clean.endswith("\n\n"))

    def test_tabs_are_preserved(self):
        """Формат Netscape тримається на табуляціях — пробіли його ламають."""
        clean, _ = BOT.strip_volatile_cookies(FILE)
        for line in clean.splitlines():
            if line.startswith("#"):
                continue
            with self.subTest(line=line[:40]):
                self.assertEqual(line.count(TAB), 6)

    def test_result_still_parses(self):
        ok, info = BOT.parse_cookies_txt(BOT.strip_volatile_cookies(FILE)[0])
        self.assertTrue(ok, "почищений файл перестав проходити валідацію")
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
    """Функція без виклику — мертвий код: файл збережеться брудним."""

    def test_both_upload_paths_use_it(self):
        """Оголошення + два шляхи прийому: файлом і вставленим текстом."""
        src = read("bot.py")
        self.assertEqual(src.count("strip_volatile_cookies("), 3,
                         "чистка не підключена до обох шляхів прийому cookies")

    def test_nothing_is_stored_unfiltered(self):
        """store_cookies має отримувати вже почищений текст."""
        src = read("bot.py")
        for handler in ("async def on_cookies_document", "async def on_cookies_text"):
            block = src[src.index(handler):]
            block = block[:block.index("store_cookies(")]
            with self.subTest(handler=handler):
                self.assertIn("strip_volatile_cookies(", block,
                              "%s: зберігає файл до чистки" % handler)

    def test_the_reason_is_written_down(self):
        """Список ключів виглядає випадковим — без пояснення його «почистять»."""
        src = read("bot.py")
        preamble = src[:src.index("_VOLATILE_COOKIES = ")][-1500:]
        self.assertIn("403", preamble, "не пояснено, чому ці ключі шкідливі")


if __name__ == "__main__":
    unittest.main(verbosity=2)
