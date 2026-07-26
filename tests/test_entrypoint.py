# -*- coding: utf-8 -*-
"""Upgrading yt-dlp when the container starts.

Channels (stable / nightly / master) must install through the documented
methods. The "yt_dlp-0.0.0-py3-none-any.whl" placeholder wheel no longer
exists in the build repos and returns 404 — and a failed upgrade is silent
by nature: the bot simply runs an old version, and broken extractors stay
broken until somebody reads the log.
"""
import re
import subprocess
import unittest

from helper import ROOT, read

SCRIPT = ROOT / "entrypoint.sh"

# Without this extra yt-dlp cannot mimic a browser's TLS fingerprint,
# and TikTok answers 403 Forbidden even to a request carrying valid cookies.
SPEC = "yt-dlp[default,curl-cffi]"


def case_block():
    """The real case from entrypoint.sh — not a copy kept in the test.

    A copy would drift: the script gets changed and the test keeps checking
    something that is no longer in it.
    """
    script = read("entrypoint.sh")
    start = script.index('case "$CHAN" in')
    return script[start:script.index("esac", start) + 4]


def pip_args(channel):
    """What exactly entrypoint hands to pip for a given channel."""
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
    """Every channel must install through its documented method."""

    def test_stable_is_plain_pypi(self):
        self.assertEqual(pip_args("stable"), [SPEC])

    def test_unset_falls_back_to_stable(self):
        self.assertEqual(pip_args(None), [SPEC])

    def test_nightly_uses_prereleases(self):
        args = pip_args("nightly")
        self.assertIn("--pre", args, "nightly lives on PyPI as a pre-release")
        self.assertIn(SPEC, args)

    def test_master_uses_a_source_tarball(self):
        args = pip_args("master")
        spec = args[0]
        self.assertIn("archive/refs/heads/master.tar.gz", spec)
        self.assertIn("github.com/yt-dlp/yt-dlp/", spec)
        # there is no git in the image — a git+ spec would not work here
        self.assertNotIn("git+", spec)


class TestImpersonation(unittest.TestCase):
    """The browser TLS fingerprint.

    TikTok filters out clients whose TLS handshake does not look like a
    browser's. yt-dlp can do this, but only with curl_cffi alongside it —
    otherwise the log fills with "attempting impersonation, but no
    impersonate target is available" and the request ends in 403 Forbidden.
    Cookies make no difference: the error is exactly the same with them.
    """

    def test_every_channel_keeps_the_extra(self):
        for channel in ("stable", "nightly", "master", None):
            with self.subTest(channel=channel or "unset"):
                joined = " ".join(pip_args(channel))
                self.assertIn("curl-cffi", joined,
                              "an upgrade would drop impersonation — TikTok starts 403ing")

    def test_image_ships_it_too(self):
        """The startup upgrade can be off — then everything rests on the image."""
        self.assertIn("curl-cffi", read("requirements.txt"),
                      "with AUTO_UPGRADE_YTDLP=0 the image would have no impersonation")

    def test_reason_is_written_down(self):
        """The extra looks optional — without a reason someone will remove it."""
        for f in ("entrypoint.sh", "requirements.txt"):
            with self.subTest(file=f):
                self.assertIn("403", read(f), "%s: does not say why curl-cffi is there" % f)


class TestNoDeadLinks(unittest.TestCase):
    def test_placeholder_wheel_is_not_used_anymore(self):
        """This is the very link that used to return 404."""
        script = read("entrypoint.sh")
        code = "\n".join(l for l in script.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("yt_dlp-0.0.0", code,
                         "the dead placeholder wheel link is back")
        self.assertNotIn("nightly-builds", code)
        self.assertNotIn("master-builds", code)

    def test_failure_is_loud(self):
        """A silent failure = the bot sits on an old version for years unnoticed."""
        script = read("entrypoint.sh")
        self.assertIn("FAILED", script)
        self.assertIn("НЕ ВДАЛОСЬ", script)

    def test_version_is_logged_either_way(self):
        script = read("entrypoint.sh")
        self.assertGreaterEqual(script.count("yt-dlp --version"), 2,
                                "the version must be shown on success and on failure")


class TestDockerfileMatches(unittest.TestCase):
    def test_entrypoint_is_copied_and_executable(self):
        dockerfile = read("Dockerfile")
        self.assertIn("entrypoint.sh", dockerfile)
        self.assertIn("chmod +x entrypoint.sh", dockerfile)

    def test_local_bin_is_in_path(self):
        """The upgraded yt-dlp lands in ~/.local — without PATH nobody sees it."""
        self.assertIn("/root/.local/bin", read("Dockerfile"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
