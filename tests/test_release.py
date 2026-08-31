# -*- coding: utf-8 -*-
"""Publishing the image: workflow, tags, compose pointed at a ready image."""
import re
import unittest

import yaml

from helper import ROOT, read

IMAGE = "ghcr.io/tameyanderson/sdp-downloader"
COMPOSE = ("docker-compose.yml", "docker-compose.lite.yml")


def workflow(name):
    """GitHub Actions: YAML reads the `on` key as boolean True — work around it."""
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
        self.assertEqual(perms.get("packages"), "write",
                         "without this the image cannot be pushed")
        self.assertEqual(perms.get("contents"), "write",
                         "without this the release is not created")

    def test_builds_for_arm_too(self):
        """Raspberry Pi and ARM VPS are a typical home for self-hosted things."""
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
        self.assertLess(smoke, release,
                        "the release must not come out before the image is checked")

        script = self.job["steps"][smoke]["run"]
        for tool in ("ffmpeg", "ffprobe", "deno", "yt-dlp", "aiogram",
                     "--list-impersonate-targets"):
            self.assertIn(tool, script, "the smoke test does not check %s" % tool)

    def test_a_tag_is_always_produced(self):
        """An empty tag list kills the build with a message about nothing.

        semver wants three parts, so a two-part tag like v1.1 makes every
        `type=semver` pattern yield nothing at all, and buildx then fails with
        "tag is needed when pushing to registry". `type=ref,event=tag` takes
        the git tag verbatim and keeps the list non-empty whatever happens.
        """
        step = next(s for s in self.job["steps"] if s.get("id") == "meta")
        tags = step["with"]["tags"]
        self.assertIn("type=ref,event=tag", tags,
                      "a non-semver tag would produce an empty list")
        self.assertIn("type=raw,value=edge", tags, "a push to main needs a tag too")

    def test_empty_tag_list_fails_loudly(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        guard = next(i for i, n in enumerate(names) if "something to tag" in n.lower())
        build = next(i for i, n in enumerate(names) if n == "Build and push")
        self.assertLess(guard, build, "the check must run before the build")
        self.assertIn("v1.2.3", self.job["steps"][guard]["run"],
                      "the error should say what a correct tag looks like")

    def test_release_only_on_tags(self):
        step = next(s for s in self.job["steps"] if "release" in s.get("name", "").lower())
        self.assertIn("refs/tags/v", step["if"])

    def test_no_secrets_beyond_the_builtin_token(self):
        raw = read(".github/workflows/release.yml")
        used = set(re.findall(r"secrets\.(\w+)", raw))
        self.assertEqual(used, {"GITHUB_TOKEN"},
                         "the workflow must run with no manual secrets: %s" % used)

    def test_image_name_is_lowercased(self):
        """A GitHub login may have capitals; Docker does not accept them."""
        raw = read(".github/workflows/release.yml")
        self.assertIn("tr '[:upper:]' '[:lower:]'", raw,
                      "the image name is not lowercased")
        self.assertNotIn("IMAGE_NAME: ${{ github.repository_owner }}", raw,
                         "the name is taken as is — with capitals the build fails")

    def test_image_name_step_runs_before_it_is_used(self):
        names = [s.get("name", "") for s in self.job["steps"]]
        made = next(i for i, n in enumerate(names) if "image name" in n.lower())
        used = next(i for i, n in enumerate(names) if "tags" in n.lower())
        self.assertLess(made, used, "the name is used before it is computed")


class TestComposeUsesPublishedImage(unittest.TestCase):
    def test_both_stacks_pull_the_image(self):
        for f in COMPOSE:
            with self.subTest(file=f):
                svc = yaml.safe_load(read(f))["services"]["video-bot"]
                self.assertIn(IMAGE, str(svc.get("image", "")),
                              "%s does not pull the published image" % f)
                self.assertNotIn("build", svc,
                                 "%s still builds locally" % f)

    def test_image_tag_is_overridable(self):
        """A Git stack in Portainer cannot be edited by hand — needs a variable."""
        for f in COMPOSE:
            with self.subTest(file=f):
                image = yaml.safe_load(read(f))["services"]["video-bot"]["image"]
                self.assertTrue(image.startswith("${SDP_IMAGE:-"),
                                "%s: the image tag cannot be overridden" % f)
                self.assertIn(":latest}", image, "%s: no default value" % f)

    def test_edge_tag_is_explained(self):
        """:latest only appears on a v* tag — :edge must be known in advance."""
        for f in COMPOSE:
            with self.subTest(file=f):
                self.assertIn(":edge", read(f),
                              "%s: does not say where to get main from" % f)

    def test_building_from_source_is_documented(self):
        for f in COMPOSE:
            with self.subTest(file=f):
                self.assertIn("# build: .", read(f),
                              "%s: no hint on how to build from source" % f)

    def test_readme_matches_the_image_name(self):
        for doc in ("README.md", "README.uk.md"):
            with self.subTest(doc=doc):
                text = read(doc)
                self.assertNotIn("up -d --build", text,
                                 "%s still tells people to build the image" % doc)
                self.assertIn("docker compose pull", text)

    def test_install_links_point_at_real_files(self):
        """The curl links in README must point at files that exist in the repo."""
        for doc in ("README.md", "README.uk.md"):
            text = read(doc)
            for m in re.finditer(r"raw\.githubusercontent\.com/[\w-]+/[\w-]+/main/(\S+)", text):
                with self.subTest(doc=doc, file=m.group(1)):
                    self.assertTrue((ROOT / m.group(1)).exists(),
                                    "README links to a missing %s" % m.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
