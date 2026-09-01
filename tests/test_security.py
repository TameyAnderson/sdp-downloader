# -*- coding: utf-8 -*-
"""Things a stranger can reach through the bot.

The bot takes a link from whoever is in the chat and hands it to a downloader.
That is the whole product, and it is also the whole attack surface: the link
decides where the server makes a request, and the request is made from inside
the network the server sits in.
"""
import os
import tempfile
import unittest

from helper import load_bot, read

BOT = load_bot()

# Addresses that must never be fetched on somebody else's behalf.
INSIDE = [
    "169.254.169.254",   # cloud metadata: hands out credentials to anyone inside
    "127.0.0.1",         # the container itself
    "::1",
    "10.0.0.5",          # private ranges — the rest of the home network
    "192.168.1.1",       # the router admin panel, typically
    "172.17.0.2",        # a neighbouring Docker container
    "0.0.0.0",
    "224.0.0.1",         # multicast
]

OUTSIDE = ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]


class TestAddressClassification(unittest.TestCase):
    def test_internal_addresses_are_rejected(self):
        for ip in INSIDE:
            with self.subTest(ip=ip):
                self.assertFalse(BOT._addr_is_public(ip))

    def test_public_addresses_pass(self):
        for ip in OUTSIDE:
            with self.subTest(ip=ip):
                self.assertTrue(BOT._addr_is_public(ip))

    def test_nonsense_is_not_public(self):
        for junk in ("", "not-an-ip", "999.999.999.999"):
            with self.subTest(value=junk):
                self.assertFalse(BOT._addr_is_public(junk))


class TestLinkFiltering(unittest.IsolatedAsyncioTestCase):
    """An IP literal needs no DNS, so these run without a network."""

    async def test_links_into_the_network_are_refused(self):
        for ip in INSIDE:
            host = "[%s]" % ip if ":" in ip else ip
            with self.subTest(ip=ip):
                self.assertFalse(await BOT.url_is_safe("http://%s/latest/meta-data/" % host))

    async def test_ordinary_links_pass(self):
        self.assertTrue(await BOT.url_is_safe("http://93.184.216.34/video"))

    async def test_only_http_is_allowed(self):
        for url in ("file:///etc/passwd", "ftp://192.168.1.1/x",
                    "gopher://127.0.0.1:11211/", "data:text/plain,hi"):
            with self.subTest(url=url):
                self.assertFalse(await BOT.url_is_safe(url))

    async def test_garbage_is_refused_not_crashed_on(self):
        for url in ("", "http://", "not a url at all"):
            with self.subTest(url=url):
                self.assertFalse(await BOT.url_is_safe(url))

    async def test_our_own_cobalt_is_still_reachable(self):
        """Self-hosted Cobalt answers with links to itself, on a private address.

        Blocking those outright would leave the whole Cobalt engine dead — the
        check has to know the difference between our own services and the rest
        of the network.
        """
        bot = load_bot(COBALT_API_URL="http://cobalt-api:9010")
        self.assertTrue(await bot.url_is_safe("http://cobalt-api:9010/tunnel?id=1"))

    async def test_the_escape_hatch_works(self):
        bot = load_bot(ALLOW_PRIVATE_HOSTS=1)
        self.assertTrue(await bot.url_is_safe("http://192.168.1.1/whatever"))


class TestItIsWiredIn(unittest.TestCase):
    def test_incoming_links_are_checked(self):
        src = read("bot.py")
        block = src[src.index("async def process_url"):]
        block = block[:block.index("async with _user_gate")]
        self.assertIn("await url_is_safe(url)", block,
                      "a link goes to the engines unchecked")

    def test_links_returned_by_other_services_are_checked_too(self):
        """Cobalt and tikwm answer with URLs; those come from outside as well."""
        src = read("bot.py")
        block = src[src.index("async def download_file"):]
        block = block[:block.index("try:")]
        self.assertIn("url_is_safe(url)", block)


class TestSecretsCanLiveInFiles(unittest.TestCase):
    """Values in the environment show up in `docker inspect` and in Portainer."""

    def test_the_file_wins_over_the_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            with open(path, "w", encoding="utf-8") as f:
                f.write("from-the-file\n")
            bot = load_bot(BOT_TOKEN="from-the-env", BOT_TOKEN_FILE=path)
            self.assertEqual(bot.BOT_TOKEN, "from-the-file")

    def test_trailing_newline_is_stripped(self):
        """A file written by hand almost always ends with one."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            with open(path, "w", encoding="utf-8") as f:
                f.write("  123:ABC  \n\n")
            bot = load_bot(BOT_TOKEN_FILE=path)
            self.assertEqual(bot.BOT_TOKEN, "123:ABC")

    def test_the_variable_still_works(self):
        self.assertEqual(load_bot(BOT_TOKEN="plain").BOT_TOKEN, "plain")

    def test_a_missing_file_does_not_take_the_bot_down(self):
        bot = load_bot(BOT_TOKEN="fallback", BOT_TOKEN_FILE="/nope/missing")
        self.assertEqual(bot.BOT_TOKEN, "fallback")

    def test_both_secrets_go_through_it(self):
        src = read("bot.py")
        self.assertIn('BOT_TOKEN = env_secret("BOT_TOKEN")', src)
        self.assertIn('GITHUB_TOKEN = env_secret("GITHUB_TOKEN")', src)


class TestPanelRateLimit(unittest.TestCase):
    """Signed initData cannot be forged, but it can be replayed for its whole TTL."""

    def test_the_middleware_is_installed(self):
        src = read("bot.py")
        self.assertIn("web.Application(middlewares=[rate_limit])", src)

    def test_only_the_api_is_limited(self):
        """/health is what the container healthcheck hits every few seconds."""
        src = read("bot.py")
        block = src[src.index("async def rate_limit"):]
        block = block[:block.index("app = web.Application")]
        self.assertIn('request.path.startswith("/api/")', block)
        self.assertIn("429", block)

    def test_unverified_callers_share_one_bucket(self):
        src = read("bot.py")
        block = src[src.index("async def rate_limit"):]
        block = block[:block.index("app = web.Application")]
        self.assertIn('"anon"', block,
                      "an unauthenticated flood would spend the admin's allowance")

    def test_the_limit_is_configurable(self):
        self.assertGreater(BOT.API_RATE_LIMIT, 0)
        self.assertGreater(BOT.API_RATE_WINDOW, 0)
        self.assertEqual(load_bot(API_RATE_LIMIT=5).API_RATE_LIMIT, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
