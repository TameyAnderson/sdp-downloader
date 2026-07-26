# -*- coding: utf-8 -*-
"""Оновлення yt-dlp при старті контейнера.

Канали (stable / nightly / master) мають ставитись задокументованими
способами. Посилання на wheel-заглушку «yt_dlp-0.0.0-py3-none-any.whl»
у збіркових репозиторіях більше не існує й повертає 404 — а помилка
оновлення тиха за своєю природою: бот просто працює зі старою версією,
і зламані екстрактори лишаються зламаними, поки хтось не загляне в лог.
"""
import re
import subprocess
import unittest

from helper import ROOT, read

SCRIPT = ROOT / "entrypoint.sh"

# Без цієї екстри yt-dlp не вміє вдавати TLS-відбиток браузера,
# і TikTok відповідає 403 Forbidden навіть на запит із дійсними cookies.
SPEC = "yt-dlp[default,curl-cffi]"


def case_block():
    """Справжній case з entrypoint.sh — не копія в тесті.

    Копія жила б власним життям: скрипт міняють, тест і далі перевіряє
    те, чого в ньому вже немає.
    """
    script = read("entrypoint.sh")
    start = script.index('case "$CHAN" in')
    return script[start:script.index("esac", start) + 4]


def pip_args(channel):
    """Що саме entrypoint передасть у pip для заданого каналу."""
    probe = 'CHAN="${YTDLP_CHANNEL:-stable}"\n%s\nfor a in "$@"; do echo "$a"; done\n' % case_block()
    env = {"YTDLP_CHANNEL": channel} if channel else {}
    out = subprocess.run(["sh", "-c", probe], capture_output=True, text=True, env=env)
    return [l for l in out.stdout.splitlines() if l]


class TestScriptIsValid(unittest.TestCase):
    def test_shell_syntax(self):
        r = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_starts_the_bot_last(self):
        self.assertTrue(read("entrypoint.sh").rstrip().endswith("exec python -u bot.py"))


class TestChannels(unittest.TestCase):
    """Кожен канал має ставитись задокументованим способом."""

    def test_stable_is_plain_pypi(self):
        self.assertEqual(pip_args("stable"), [SPEC])

    def test_unset_falls_back_to_stable(self):
        self.assertEqual(pip_args(None), [SPEC])

    def test_nightly_uses_prereleases(self):
        args = pip_args("nightly")
        self.assertIn("--pre", args, "nightly живе на PyPI як pre-release")
        self.assertIn(SPEC, args)

    def test_master_uses_a_source_tarball(self):
        args = pip_args("master")
        spec = args[0]
        self.assertIn("archive/refs/heads/master.tar.gz", spec)
        self.assertIn("github.com/yt-dlp/yt-dlp/", spec)
        # git у образі немає — специфікація git+ тут не спрацює
        self.assertNotIn("git+", spec)


class TestImpersonation(unittest.TestCase):
    """TLS-відбиток браузера.

    TikTok відсіює клієнтів, чиє TLS-рукостискання не схоже на браузерне.
    yt-dlp це вміє, але тільки коли поруч стоїть curl_cffi — інакше в лог
    падає «attempting impersonation, but no impersonate target is available»,
    а сам запит завершується 403 Forbidden. Cookies на це не впливають:
    з ними помилка та сама.
    """

    def test_every_channel_keeps_the_extra(self):
        for channel in ("stable", "nightly", "master", None):
            with self.subTest(channel=channel or "unset"):
                joined = " ".join(pip_args(channel))
                self.assertIn("curl-cffi", joined,
                              "оновлення викине імперсонацію — TikTok почне бити 403")

    def test_image_ships_it_too(self):
        """Апгрейд при старті вимикається — тоді все тримається на образі."""
        self.assertIn("curl-cffi", read("requirements.txt"),
                      "з AUTO_UPGRADE_YTDLP=0 в образі не буде імперсонації")

    def test_reason_is_written_down(self):
        """Екстра виглядає необов'язковою — без пояснення її колись приберуть."""
        for f in ("entrypoint.sh", "requirements.txt"):
            with self.subTest(file=f):
                self.assertIn("403", read(f), "%s: не пояснено, навіщо curl-cffi" % f)


class TestNoDeadLinks(unittest.TestCase):
    def test_placeholder_wheel_is_not_used_anymore(self):
        """Саме це посилання й повертало 404."""
        script = read("entrypoint.sh")
        code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("yt_dlp-0.0.0", code,
                         "повернулось мертве посилання на wheel-заглушку")
        self.assertNotIn("nightly-builds", code)
        self.assertNotIn("master-builds", code)

    def test_failure_is_loud(self):
        """Тиха помилка = бот роками сидить на старій версії й ніхто не знає."""
        script = read("entrypoint.sh")
        self.assertIn("FAILED", script)
        self.assertIn("НЕ ВДАЛОСЬ", script)

    def test_version_is_logged_either_way(self):
        script = read("entrypoint.sh")
        self.assertGreaterEqual(script.count("yt-dlp --version"), 2,
                                "версію треба показувати і при успіху, і при збої")


class TestDockerfileMatches(unittest.TestCase):
    def test_entrypoint_is_copied_and_executable(self):
        dockerfile = read("Dockerfile")
        self.assertIn("entrypoint.sh", dockerfile)
        self.assertIn("chmod +x entrypoint.sh", dockerfile)

    def test_local_bin_is_in_path(self):
        """Оновлений yt-dlp лягає в ~/.local — без PATH його ніхто не побачить."""
        self.assertIn("/root/.local/bin", read("Dockerfile"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
