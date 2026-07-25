# -*- coding: utf-8 -*-
"""Конфіги деплою: саме тут ловились дублікати ключів і загублені змінні."""
import re
import unittest
from pathlib import Path

import yaml

from helper import ROOT, read

COMPOSE = ("docker-compose.yml", "docker-compose.lite.yml")


class StrictLoader(yaml.SafeLoader):
    """Той самий YAML, але з падінням на дубльованому ключі — як у Portainer."""


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for k, _v in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise AssertionError("дубльований ключ: %r" % key)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load(name):
    return yaml.load(read(name), Loader=StrictLoader)


class TestCompose(unittest.TestCase):
    def test_parses_without_duplicate_keys(self):
        for f in COMPOSE:
            with self.subTest(file=f):
                self.assertIn("services", load(f))

    def test_every_env_var_is_read_by_the_code(self):
        src = read("bot.py")
        known = set(re.findall(r'os\.getenv\("([A-Z_0-9]+)"', src))
        known |= {"ENABLE_" + s.upper() for s in re.findall(r'^    "(\w+)": \{', src, re.M)}
        known |= {"YTDLP_CHANNEL", "AUTO_UPGRADE_YTDLP"}          # читає entrypoint.sh
        for f in COMPOSE:
            env = load(f)["services"]["video-bot"]["environment"]
            unused = sorted(k for k in env if k not in known)
            self.assertFalse(unused, "%s: змінні, яких ніхто не читає: %s" % (f, unused))

    def test_lite_stack_is_actually_lite(self):
        lite = load("docker-compose.lite.yml")
        services = lite["services"]
        always_on = [k for k, v in services.items() if not v.get("profiles")]
        self.assertEqual(sorted(always_on), ["cobalt-api", "video-bot"])
        self.assertNotIn("volumes", lite, "LITE не має томів — він нічого не зберігає")

        env = services["video-bot"]["environment"]
        self.assertEqual(env["LITE"], "1")
        for forbidden in ("STATS_DB", "WEBAPP_ENABLED", "TELEGRAM_API_URL", "CACHE_FILE"):
            self.assertNotIn(forbidden, env, "у LITE не місце для %s" % forbidden)

    def test_full_stack_has_the_optional_profiles(self):
        services = load("docker-compose.yml")["services"]
        self.assertEqual(services["telegram-bot-api"].get("profiles"), ["bigfiles"])
        self.assertEqual(services["cloudflared"].get("profiles"), ["miniapp"])


class TestDockerfile(unittest.TestCase):
    def test_copied_files_are_not_excluded(self):
        import fnmatch
        patterns = [l.strip() for l in read(".dockerignore").splitlines()
                    if l.strip() and not l.startswith("#")]
        copied = []
        for line in read("Dockerfile").splitlines():
            if line.startswith("COPY ") and "--from" not in line:
                copied += line.split()[1:-1]
        self.assertTrue(copied)
        for f in copied:
            ignored = False
            for p in patterns:
                neg = p.startswith("!")
                if fnmatch.fnmatch(f, p[1:] if neg else p):
                    ignored = not neg
            self.assertFalse(ignored, "%s потрібен в образі, але його ріже .dockerignore" % f)


class TestSecrets(unittest.TestCase):
    PATTERNS = {
        "токен Telegram": r"\b\d{8,12}:[A-Za-z0-9_-]{35,}\b",
        "GitHub PAT": r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        "приватний ключ": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "токен Cloudflare": r"\beyJhIjoi[A-Za-z0-9_\-.=]{20,}",
        "cookie сесії": r"sessionid=[A-Za-z0-9%]{15,}",
    }

    def test_no_secrets_in_tracked_files(self):
        bad = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix.lower() in (".png", ".jpg", ".mp4", ".db", ".pyc"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name, pat in self.PATTERNS.items():
                for m in re.finditer(pat, text):
                    if "xxxx" in m.group(0).lower() or m.group(0).startswith("123456789:AA"):
                        continue                      # плейсхолдери в прикладах
                    bad.append("%s: %s" % (path.name, name))
        self.assertFalse(bad, "схоже на секрет у репозиторії: %s" % bad)

    def test_gitignore_covers_the_dangerous_files(self):
        rules = read(".gitignore")
        for needed in ("cookies", ".env", "*.db", "cache.json"):
            self.assertIn(needed, rules, "%s має бути в .gitignore" % needed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
