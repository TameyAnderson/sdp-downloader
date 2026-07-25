# -*- coding: utf-8 -*-
"""Публікація образу: workflow, теги, compose під готовий образ."""
import re
import unittest

import yaml

from helper import ROOT, read

IMAGE = "ghcr.io/tameyanderson/sdp-downloader"
COMPOSE = ("docker-compose.yml", "docker-compose.lite.yml")


def workflow(name):
    """GitHub Actions: ключ `on` YAML читає як булеве True — обходимо це."""
    data = yaml.safe_load(read(".github/workflows/%s" % name))
    if True in data:
        data["on"] = data.pop(True)
    return data


class TestReleaseWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = workflow("release.yml")
        self.job = self.wf["jobs"]["build"]

    def test_triggers_on_tags_and_main(self):
        push = self.wf["on"]["push"]
        self.assertIn("main", push["branches"])
        self.assertIn("v*", push["tags"])

    def test_has_the_permissions_it_needs(self):
        perms = self.job["permissions"]
        self.assertEqual(perms.get("packages"), "write", "без цього образ не запушиться")
        self.assertEqual(perms.get("contents"), "write", "без цього не створиться реліз")

    def test_builds_for_arm_too(self):
        """Raspberry Pi та ARM-VPS — типовий дім для self-hosted."""
        step = next(s for s in self.job["steps"]
                    if str(s.get("uses", "")).startswith("docker/build-push-action"))
        platforms = step["with"]["platforms"]
        self.assertIn("linux/amd64", platforms)
        self.assertIn("linux/arm64", platforms)
        self.assertTrue(step["with"]["push"])

    def test_uses_layer_cache(self):
        step = next(s for s in self.job["steps"]
                    if str(s.get("uses", "")).startswith("docker/build-push-action"))
        self.assertIn("cache-from", step["with"])
        self.assertIn("cache-to", step["with"])

    def test_image_is_smoke_tested_before_release(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        smoke = next(i for i, n in enumerate(names) if "Smoke" in n)
        release = next(i for i, n in enumerate(names) if "release" in n.lower())
        self.assertLess(smoke, release, "реліз не має виходити раніше за перевірку образу")

        script = self.job["steps"][smoke]["run"]
        for tool in ("ffmpeg", "ffprobe", "deno", "yt-dlp", "aiogram"):
            self.assertIn(tool, script, "smoke-тест не перевіряє %s" % tool)

    def test_release_only_on_tags(self):
        step = next(s for s in self.job["steps"] if "release" in s.get("name", "").lower())
        self.assertIn("refs/tags/v", step["if"])

    def test_no_secrets_beyond_the_builtin_token(self):
        raw = read(".github/workflows/release.yml")
        used = set(re.findall(r"secrets\.(\w+)", raw))
        self.assertEqual(used, {"GITHUB_TOKEN"},
                         "workflow має працювати без ручних секретів: %s" % used)

    def test_image_name_is_lowercased(self):
        """Логін на GitHub може мати великі літери, Docker їх не приймає."""
        raw = read(".github/workflows/release.yml")
        self.assertIn("tr '[:upper:]' '[:lower:]'", raw,
                      "назва образу не зводиться до нижнього регістру")
        self.assertNotIn("IMAGE_NAME: ${{ github.repository_owner }}", raw,
                         "назва береться як є — з великими літерами збірка впаде")

    def test_image_name_step_runs_before_it_is_used(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        made = next(i for i, n in enumerate(names) if "image name" in n.lower())
        used = next(i for i, n in enumerate(names) if "tags" in n.lower())
        self.assertLess(made, used, "назву використали раніше, ніж порахували")


class TestComposeUsesPublishedImage(unittest.TestCase):
    def test_both_stacks_pull_the_image(self):
        for f in COMPOSE:
            with self.subTest(file=f):
                svc = yaml.safe_load(read(f))["services"]["video-bot"]
                self.assertIn(IMAGE, str(svc.get("image", "")),
                              "%s не тягне опублікований образ" % f)
                self.assertNotIn("build", svc,
                                 "%s досі збирає локально" % f)

    def test_image_tag_is_overridable(self):
        """Git-стек у Portainer не редагується вручну — має бути змінна."""
        for f in COMPOSE:
            with self.subTest(file=f):
                image = yaml.safe_load(read(f))["services"]["video-bot"]["image"]
                self.assertTrue(image.startswith("${SDP_IMAGE:-"),
                                "%s: тег образу не перевизначити змінною" % f)
                self.assertIn(":latest}", image, "%s: немає значення за замовчуванням" % f)

    def test_edge_tag_is_explained(self):
        """:latest з'являється лише на тег v* — про :edge треба знати заздалегідь."""
        for f in COMPOSE:
            with self.subTest(file=f):
                self.assertIn(":edge", read(f), "%s: не пояснено, звідки брати main" % f)

    def test_building_from_source_is_documented(self):
        for f in COMPOSE:
            with self.subTest(file=f):
                self.assertIn("# build: .", read(f),
                              "%s: немає підказки, як зібрати з коду" % f)

    def test_readme_matches_the_image_name(self):
        for doc in ("README.md", "README.uk.md"):
            with self.subTest(doc=doc):
                text = read(doc)
                self.assertNotIn("up -d --build", text,
                                 "%s досі радить збирати образ" % doc)
                self.assertIn("docker compose pull", text)

    def test_install_links_point_at_real_files(self):
        """curl-посилання з README мають вести на файли, що існують у репо."""
        for doc in ("README.md", "README.uk.md"):
            text = read(doc)
            for m in re.finditer(r"raw\.githubusercontent\.com/[\w-]+/[\w-]+/main/(\S+)", text):
                with self.subTest(doc=doc, file=m.group(1)):
                    self.assertTrue((ROOT / m.group(1)).exists(),
                                    "у README посилання на неіснуючий %s" % m.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
