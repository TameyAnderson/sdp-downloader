# -*- coding: utf-8 -*-
"""Docs: both languages, working links, no leftovers of old names."""
import re
import unittest
from pathlib import Path

from helper import ROOT, read

# English file -> its Ukrainian counterpart
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
                self.assertTrue((ROOT / en).exists(), "no English version: %s" % en)
                self.assertTrue((ROOT / uk).exists(), "no Ukrainian version: %s" % uk)

    def test_language_switchers(self):
        """Each version must link to the other language."""
        for en, uk in PAIRS:
            with self.subTest(doc=en):
                name_uk = Path(uk).name
                name_en = Path(en).name
                self.assertIn(name_uk, read(en), "%s: no switch to Ukrainian" % en)
                self.assertIn(name_en, read(uk), "%s: no switch to English" % uk)

    def test_english_version_is_actually_english(self):
        for en, _uk in PAIRS:
            with self.subTest(doc=en):
                text = read(en)
                # drop the language switcher line and the .uk file names
                text = re.sub(r"<p>.*?</p>", "", text, flags=re.S)
                text = re.sub(r'<p align="center">.*?</p>', "", text, flags=re.S)
                found = CYRILLIC.findall(text)
                self.assertFalse(found, "%s: Cyrillic in the English version (%d chars)"
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
        """The files live in docs/ and are really referenced, with no leftover markers."""
        import re
        shots = sorted(x.name for x in (ROOT / "docs").glob("*.png"))
        self.assertTrue(shots, "docs/ holds no screenshots at all")
        for doc in ("README.md", "README.uk.md"):
            with self.subTest(doc=doc):
                text = read(doc)
                used = set(re.findall(r'src="docs/([^"]+)"', text))
                self.assertTrue(used, "%s: no screenshot embedded" % doc)
                for name in used:
                    self.assertIn(name, shots, "%s: link to a missing %s" % (doc, name))
                self.assertNotIn("<!-- SCREENSHOT", text, "%s: a marker was left" % doc)
                self.assertNotIn("<!-- СКРІНШОТ", text, "%s: a marker was left" % doc)

    def test_screenshot_files_are_named_plainly(self):
        """Double extensions (.png.png) break the links in README."""
        for shot in (ROOT / "docs").glob("*.png*"):
            with self.subTest(file=shot.name):
                self.assertFalse(shot.name.endswith(".png.png"),
                                 "double extension: %s" % shot.name)

    def test_docs_holds_images_only(self):
        """docs/ is the shop window for README; working notes live in notes/."""
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "docs"], cwd=str(ROOT),
                                 capture_output=True, text=True).stdout.split()
        stray = [f for f in tracked if not f.endswith(".png")]
        self.assertFalse(stray, "something stray got into docs/: %s" % stray)


class TestBilingualConfigs(unittest.TestCase):
    """In configs every meaningful comment must appear in both languages."""

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
                self.assertTrue(ukr, "%s: no Ukrainian comments" % f)
                self.assertTrue(eng, "%s: no English comments" % f)
                # crude but effective balance check: neither language may dominate
                ratio = len(eng) / max(1, len(ukr))
                self.assertGreater(ratio, 0.4, "%s: too few English comments" % f)
                self.assertLess(ratio, 2.5, "%s: too few Ukrainian comments" % f)


class TestNoStaleNames(unittest.TestCase):
    """Dropped engines and renamed files must not survive in the texts."""

    GONE = ("gallery-dl", "gallerydl", "IG-embed", "ENABLE_ARIA2",
            "ПІДСУМКИ_та_ПОРІВНЯННЯ", "AGPL")

    def test_removed_things_are_not_mentioned(self):
        # notes/ is deliberately out of scope: local drafts, not the repository
        for path in list(ROOT.glob("*.md")) + list(ROOT.glob("*.yml")) \
                + list((ROOT / "docs").glob("*.md")) + [ROOT / ".env.example",
                                                        ROOT / ".env.lite.example"]:
            text = path.read_text(encoding="utf-8")
            # The roadmap has a "Rejected" section where dropped things are
            # mentioned on purpose, as a record of decisions. Cut it off.
            text = re.split(r"##\s+\d*\.?\s*(Rejected|Відхилено)", text)[0]
            for gone in self.GONE:
                with self.subTest(file=path.name, term=gone):
                    self.assertNotIn(gone, text,
                                     "%s still mentions %s" % (path.name, gone))

    def test_license_is_mit(self):
        lic = read("LICENSE")
        self.assertIn("MIT License", lic)
        self.assertIn("TameyAnderson", lic)


class TestRepoIsClean(unittest.TestCase):
    """A public repository holds only what is needed to run and to understand."""

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
        self.assertFalse(extra, "stray files in the repository root: %s" % extra)

    def test_working_notes_are_not_published(self):
        tracked = self.tracked()
        for f in tracked:
            with self.subTest(file=f):
                self.assertFalse(f.startswith("notes/"),
                                 "drafts must not reach the repository: %s" % f)

    def test_requirements_is_present(self):
        """The image will not build without it — the file is mandatory."""
        self.assertIn("requirements.txt", self.tracked())
        self.assertIn("requirements.txt", read("Dockerfile"))

    def test_banner_is_where_readme_expects_it(self):
        self.assertIn('src="docs/banner.png"', read("README.md"))
        self.assertTrue((ROOT / "docs" / "banner.png").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
