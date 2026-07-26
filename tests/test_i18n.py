# -*- coding: utf-8 -*-
"""Bilingual: not a single label may be left untranslated."""
import re
import unittest

from helper import load_bot, read

BOT = load_bot()


def app_dicts():
    """The uk/en dictionaries out of index.html."""
    html = read("index.html")
    uk = re.search(r"\n      uk:\s*\{(.*?)\n      \},", html, re.S)
    en = re.search(r"\n      en:\s*\{(.*?)\n      \}", html, re.S)
    assert uk and en, "could not find the I.uk / I.en dictionaries in index.html"
    key = r"(?:^|,)\s*([a-z_][a-z0-9_]*)\s*:"
    return (set(re.findall(key, uk.group(1), re.M)),
            set(re.findall(key, en.group(1), re.M)))


class TestBotStrings(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        uk, en = set(BOT.T["uk"]), set(BOT.T["en"])
        self.assertFalse(uk - en, "missing in English: %s" % sorted(uk - en))
        self.assertFalse(en - uk, "missing in Ukrainian: %s" % sorted(en - uk))

    def test_no_untranslated_duplicates(self):
        same = [k for k in BOT.T["uk"] if BOT.T["uk"][k] == BOT.T["en"][k]
                and len(BOT.T["uk"][k]) > 12]
        self.assertFalse(same, "identical text in both languages — looks untranslated: %s" % same)

    def test_placeholders_match(self):
        """{days} in one language and {d} in the other = a crash in .format()."""
        for k in BOT.T["uk"]:
            with self.subTest(key=k):
                ph = lambda s: set(re.findall(r"\{(\w+)\}", s))
                self.assertEqual(ph(BOT.T["uk"][k]), ph(BOT.T["en"][k]),
                                 "different placeholders in key %s" % k)

    def test_every_used_key_exists(self):
        src = read("bot.py")
        used = set(re.findall(r'\bt\("(\w+)"', src))
        missing = used - set(BOT.T["uk"])
        self.assertFalse(missing, "the code uses keys that do not exist: %s" % sorted(missing))

    def test_no_hardcoded_ukrainian_in_replies(self):
        """Replies to the user must go through t(), not sit there as literals."""
        src = read("bot.py").splitlines()
        cyr = re.compile(r"[А-Яа-яЇїІіЄєҐґ]")
        bad = []
        for i, line in enumerate(src, 1):
            if not re.search(r"\.(reply|answer|send_message)\(", line):
                continue
            for lit in re.findall(r'"([^"\n]*)"', line):
                if cyr.search(lit):
                    bad.append("%d: %s" % (i, line.strip()[:70]))
        self.assertFalse(bad, "Ukrainian strings straight in the code:\n" + "\n".join(bad))


class TestMiniApp(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        uk, en = app_dicts()
        self.assertFalse(uk - en, "missing in English: %s" % sorted(uk - en))
        self.assertFalse(en - uk, "missing in Ukrainian: %s" % sorted(en - uk))

    def test_every_used_key_is_translated(self):
        html = read("index.html")
        uk, _ = app_dicts()
        used = set()
        for pat in (r'data-i18n="(\w+)"', r'data-i18n-ph="(\w+)"',
                    r'\btr\("(\w+)"\)', r'\btrf\("(\w+)"'):
            used |= set(re.findall(pat, html))
        missing = used - uk
        self.assertFalse(missing, "used but not translated: %s" % sorted(missing))

    def test_no_ukrainian_text_without_data_i18n(self):
        html = read("index.html").split("<script>")[0]
        cyr = re.compile(r"[А-Яа-яЇїІіЄєҐґ]")
        bad = []
        for tag in re.finditer(r"<(\w+)([^>]*)>([^<]{2,})", html):
            name, attrs, text = tag.groups()
            if name == "option":
                continue                      # language names stay as they are
            if cyr.search(text) and "data-i18n" not in attrs:
                bad.append(text.strip()[:50])
        self.assertFalse(bad, "text without data-i18n: %s" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
