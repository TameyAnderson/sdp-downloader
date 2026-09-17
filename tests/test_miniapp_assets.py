"""The component bundle is public; no other app files may be served."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from helper import load_bot, read


class TestMiniAppAssets(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "index.html").write_text("<h1>Mini App</h1>", encoding="utf-8")
        (self.root / "static").mkdir()
        (self.root / "static" / "material.js").write_text("/* test asset */", encoding="utf-8")
        self.bot = load_bot(WEBAPP_ENABLED=1, INDEX_HTML_PATH=self.root / "index.html")
        # Capture the real aiohttp application without binding its production port.
        with patch.object(self.bot.web, "AppRunner") as runner, patch.object(self.bot.web, "TCPSite") as site:
            runner.return_value.setup = AsyncMock()
            site.return_value.start = AsyncMock()
            await self.bot.start_web_server(None)
            app = runner.call_args.args[0]
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_bundle_is_public_and_revalidated(self):
        response = await self.client.get("/assets/material.js")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "/* test asset */")
        self.assertEqual(response.headers["Content-Type"], "application/javascript")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_no_directory_or_secret_access(self):
        for target in ("/assets/", "/assets/bot.py", "/assets/.env", "/assets/static/material.js"):
            with self.subTest(target=target):
                response = await self.client.get(target)
                self.assertEqual(response.status, 404)

    async def test_missing_build_is_404(self):
        (self.root / "static" / "material.js").unlink()
        response = await self.client.get("/assets/material.js")
        self.assertEqual(response.status, 404)

    async def test_api_still_requires_telegram_auth(self):
        response = await self.client.post("/api/download", json={"url": "https://youtu.be/test"})
        self.assertEqual(response.status, 403)


class TestMiniAppBuild(unittest.TestCase):
    def test_docker_builds_and_copies_components(self):
        dockerfile = read("Dockerfile")
        self.assertIn("FROM node:22-slim AS miniapp", dockerfile)
        self.assertIn("RUN npm ci", dockerfile)
        self.assertIn("RUN npm run build", dockerfile)
        self.assertIn("COPY --from=miniapp /build/static ./static", dockerfile)
        self.assertIn('localPreview ? "./static/material.js" : "/assets/material.js"', read("index.html"))


if __name__ == "__main__":
    unittest.main()
