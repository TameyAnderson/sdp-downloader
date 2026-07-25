# -*- coding: utf-8 -*-
"""Документація: двомовність, робочі посилання, ніяких залишків старих назв."""
import re
import unittest
from pathlib import Path

from helper import ROOT, read

# англійський файл -> українська пара
PAIRS = [
    ("README.md", "README.uk.md"),
    ("ARCHITECTURE.md", "ARCHITECTURE.uk.md"),
    ("CONTRIBUTING.md", "CONTRIBUTING.uk.md"),
    ("tests/README.md", "tests/README.uk.md"),
]

CYRILLIC = re.compile(r"[А-Яа-яЇїІіЄєҐґ]")


class TestBilingualDocs(unittest.TestCase):
    def test_both_versions_exist(self):
        for en, uk in PAIRS:
            with self.subTest(doc=en):
                self.assertTrue((ROOT / en).exists(), "немає англійської версії: %s" % en)
                self.assertTrue((ROOT / uk).exists(), "немає української версії: %s" % uk)

    def test_language_switchers(self):
        """У кожній версії має бути посилання на іншу мову."""
        for en, uk in PAIRS:
            with self.subTest(doc=en):
                name_uk = Path(uk).name
                name_en = Path(en).name
                self.assertIn(name_uk, read(en), "%s: немає перемикача на українську" % en)
                self.assertIn(name_en, read(uk), "%s: немає перемикача на англійську" % uk)

    def test_english_version_is_actually_english(self):
        for en, _uk in PAIRS:
            with self.subTest(doc=en):
                text = read(en)
                # прибираємо рядок перемикача мов і назви .uk-файлів
                text = re.sub(r"<p>.*?</p>", "", text, flags=re.S)
                text = re.sub(r'<p align="center">.*?</p>', "", text, flags=re.S)
                found = CYRILLIC.findall(text)
                self.assertFalse(found, "%s: кирилиця в англійській версії (%d символів)"
                                 % (en, len(found)))

    def test_no_dead_links(self):
        for md in list(ROOT.glob("*.md")) + list((ROOT / "tests").glob("*.md")) \
                + list((ROOT / "docs").glob("*.md")):
            text = md.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)#h][^)]*)\)", text):
                with self.subTest(doc=md.name, link=m.group(1)):
                    self.assertTrue((md.parent / m.group(1)).exists(),
                                    "%s -> %s" % (md.name, m.group(1)))

    def test_screenshots_are_wired_into_both_readmes(self):
        """Файли лежать у docs/ і на них справді посилаються, без залишків міток."""
        import re
        shots = sorted(x.name for x in (ROOT / "docs").glob("*.png"))
        self.assertTrue(shots, "у docs/ немає жодного скріншота")
        for doc in ("README.md", "README.uk.md"):
            with self.subTest(doc=doc):
                text = read(doc)
                used = set(re.findall(r'src="docs/([^"]+)"', text))
                self.assertTrue(used, "%s: жодного скріншота не вставлено" % doc)
                for name in used:
                    self.assertIn(name, shots, "%s: посилання на відсутній %s" % (doc, name))
                self.assertNotIn("<!-- SCREENSHOT", text, "%s: лишилась мітка" % doc)
                self.assertNotIn("<!-- СКРІНШОТ", text, "%s: лишилась мітка" % doc)

    def test_screenshot_files_are_named_plainly(self):
        """Подвійні розширення (.png.png) ламають посилання в README."""
        for shot in (ROOT / "docs").glob("*.png*"):
            with self.subTest(file=shot.name):
                self.assertFalse(shot.name.endswith(".png.png"),
                                 "подвійне розширення: %s" % shot.name)

    def test_docs_holds_images_only(self):
        """docs/ — це вітрина для README, робочі нотатки живуть у notes/."""
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "docs"], cwd=str(ROOT),
                                 capture_output=True, text=True).stdout.split()
        stray = [f for f in tracked if not f.endswith(".png")]
        self.assertFalse(stray, "у docs/ потрапило зайве: %s" % stray)


class TestBilingualConfigs(unittest.TestCase):
    """У конфігах кожен змістовний коментар має бути двома мовами."""

    FILES = ("docker-compose.yml", "docker-compose.lite.yml",
             ".env.example", ".env.lite.example", ".gitignore", "Dockerfile",
             "requirements.txt")

    def test_configs_have_both_languages(self):
        for f in self.FILES:
            with self.subTest(file=f):
                text = read(f)
                comments = [l for l in text.splitlines() if l.strip().startswith("#")]
                ukr = [c for c in comments if CYRILLIC.search(c)]
                eng = [c for c in comments
                       if not CYRILLIC.search(c) and re.search(r"[A-Za-z]{4,}", c)]
                self.assertTrue(ukr, "%s: немає українських коментарів" % f)
                self.assertTrue(eng, "%s: немає англійських коментарів" % f)
                # груба, але дієва перевірка балансу: жодна мова не має домінувати
                ratio = len(eng) / max(1, len(ukr))
                self.assertGreater(ratio, 0.4, "%s: англійських коментарів надто мало" % f)
                self.assertLess(ratio, 2.5, "%s: українських коментарів надто мало" % f)


class TestNoStaleNames(unittest.TestCase):
    """Прибрані рушії та перейменовані файли не мають лишатись у текстах."""

    GONE = ("gallery-dl", "gallerydl", "IG-embed", "ENABLE_ARIA2",
            "ПІДСУМКИ_та_ПОРІВНЯННЯ", "AGPL")

    def test_removed_things_are_not_mentioned(self):
        # notes/ навмисно поза перевіркою: це локальні чернетки, не репозиторій
        for path in list(ROOT.glob("*.md")) + list(ROOT.glob("*.yml")) \
                + list((ROOT / "docs").glob("*.md")) + [ROOT / ".env.example",
                                                        ROOT / ".env.lite.example"]:
            text = path.read_text(encoding="utf-8")
            # У роадмапі є розділ «Відхилено» — там прибрані речі згадуються
            # свідомо, як історія рішень. Відрізаємо його.
            text = re.split(r"##\s+\d*\.?\s*(Rejected|Відхилено)", text)[0]
            for gone in self.GONE:
                with self.subTest(file=path.name, term=gone):
                    self.assertNotIn(gone, text,
                                     "%s ще згадує %s" % (path.name, gone))

    def test_license_is_mit(self):
        lic = read("LICENSE")
        self.assertIn("MIT License", lic)
        self.assertIn("TameyAnderson", lic)


class TestRepoIsClean(unittest.TestCase):
    """У публічному репозиторії лише те, що потрібне для запуску й розуміння."""

    ALLOWED_ROOT = {
        "bot.py", "index.html", "requirements.txt", "Dockerfile", "entrypoint.sh",
        "docker-compose.yml", "docker-compose.lite.yml",
        ".env.example", ".env.lite.example", ".gitignore", ".dockerignore",
        "LICENSE", "README.md", "README.uk.md",
        "ARCHITECTURE.md", "ARCHITECTURE.uk.md",
        "CONTRIBUTING.md", "CONTRIBUTING.uk.md",
    }

    def tracked(self):
        import subprocess
        out = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                             capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if l]

    def test_no_unexpected_files_in_root(self):
        root_files = {f for f in self.tracked() if "/" not in f}
        extra = sorted(root_files - self.ALLOWED_ROOT)
        self.assertFalse(extra, "зайве в корені репозиторію: %s" % extra)

    def test_working_notes_are_not_published(self):
        tracked = self.tracked()
        for f in tracked:
            with self.subTest(file=f):
                self.assertFalse(f.startswith("notes/"),
                                 "чернетки не мають потрапляти в репозиторій: %s" % f)

    def test_requirements_is_present(self):
        """Без нього не збереться образ — файл обов'язковий."""
        self.assertIn("requirements.txt", self.tracked())
        self.assertIn("requirements.txt", read("Dockerfile"))

    def test_banner_is_where_readme_expects_it(self):
        self.assertIn('src="docs/banner.png"', read("README.md"))
        self.assertTrue((ROOT / "docs" / "banner.png").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
