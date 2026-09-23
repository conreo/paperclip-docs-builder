"""
Integration tests for the failure paths, against a real local git repository.

These exist because the unit tests all passed while the builder was destroying
landing pages and silently emptying bundles: the bugs only appeared when a whole
build ran. They are hermetic — `git init` in a temp directory, no network — so they
run everywhere.

Each test drives `main()` the way an operator does, so it covers the pieces that
only meet there: fetch → filter → write → manifest → swap, including what happens
to a bundle whose source stops working.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch  # noqa: E402

USER = ["-c", "user.email=test@example.invalid", "-c", "user.name=Test"]


def make_repo(root: Path, files: dict[str, str]) -> str:
    """A real git repository containing `files`. Returns its path."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *USER, "commit", "-q", "-m", "docs"], cwd=root, check=True)
    return str(root)


def write_config(path: Path, repo: str, include: str = "**/*.md") -> None:
    path.write_text(
        "version: 1\n"
        "sources:\n"
        "  demo:\n"
        "    title: Demo\n"
        "    kind: git\n"
        f"    repo: {repo}\n"
        f"    include: [\"{include}\"]\n"
        "    convert: none\n"
    )


def run_build(config: Path, workdir: Path, out: Path) -> int:
    """Run a build, swallowing its progress output so the suite stays readable."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fetch.main(
            [
                "--sources", str(config),
                "--work", str(workdir),
                "--out", str(out),
                "--jobs", "1",
                "--no-previous",
            ]
        )


def run_check(config: Path) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return fetch.main(["--sources", str(config), "--check"])


class LocalBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "sources.yaml"
        self.work = self.tmp / "work"
        self.out = self.tmp / "out" / "okf-bundles"

    def manifest(self) -> dict:
        return json.loads((self.out / fetch.MANIFEST_FILENAME).read_text())

    def test_a_normal_build_writes_pages_and_a_manifest(self):
        repo = make_repo(self.tmp / "repo", {"guide/intro.md": "# Intro\n\nbody text long enough\n"})
        write_config(self.cfg, repo)
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)

        self.assertTrue((self.out / "demo" / "guide" / "intro.md").is_file())
        self.assertTrue((self.out / "demo" / "index.md").is_file())
        entry = self.manifest()["sources"]["demo"]
        self.assertTrue(entry["commit"])
        self.assertTrue(entry["commit_date"])
        self.assertEqual(entry["pages"], 1)
        self.assertFalse(entry["stale"])

    def test_pages_reported_equal_pages_on_disk(self):
        # The bug this pins: the manifest counted pages that the writer then
        # overwrote with generated navigation, so the totals were fiction.
        repo = make_repo(
            self.tmp / "repo",
            {
                "index.md": "# Root landing\n\nreal prose\n",
                "guide/index.md": "# Guide landing\n\nreal prose\n",
                "guide/intro.md": "# Intro\n\nbody text long enough\n",
            },
        )
        write_config(self.cfg, repo)
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)

        on_disk = sorted(p.relative_to(self.out / "demo").as_posix() for p in (self.out / "demo").rglob("*.md"))
        self.assertEqual(on_disk, ["guide/index.md", "guide/intro.md", "index.md"])
        # Every page reported is a page that exists, and the landing pages kept
        # their own content rather than being replaced by a listing.
        self.assertEqual(self.manifest()["sources"]["demo"]["pages"], len(on_disk))
        guide = (self.out / "demo" / "guide" / "index.md").read_text()
        self.assertIn("real prose", guide)
        self.assertNotIn("](index.md)", guide)

    def test_a_source_that_matches_nothing_keeps_its_previous_bundle(self):
        # The drift case: upstream moves its docs, the globs stop matching, and the
        # first version silently replaced a good bundle with an empty stub.
        repo = make_repo(self.tmp / "repo", {"guide/intro.md": "# Intro\n\nbody text long enough\n"})
        write_config(self.cfg, repo)
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)
        good = (self.out / "demo" / "guide" / "intro.md").read_text()

        write_config(self.cfg, repo, include="**/*.nomatch")
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)

        self.assertTrue((self.out / "demo" / "guide" / "intro.md").is_file())
        self.assertEqual((self.out / "demo" / "guide" / "intro.md").read_text(), good)
        entry = self.manifest()["sources"]["demo"]
        self.assertTrue(entry["stale"])
        self.assertIn("no pages matched", entry["error"])
        # And the kept bundle still reports where it came from.
        self.assertTrue(entry["commit"])
        self.assertEqual(entry["pages"], 1)

    def test_an_empty_first_build_fails_rather_than_shipping_a_stub(self):
        repo = make_repo(self.tmp / "repo", {"guide/intro.md": "# Intro\n\nbody text long enough\n"})
        write_config(self.cfg, repo, include="**/*.nomatch")
        self.assertEqual(run_build(self.cfg, self.work, self.out), 1)
        self.assertFalse((self.out / "demo").exists())

    def test_changing_the_repo_rebuilds_from_the_new_one(self):
        # A cache keyed by source name is not a cache of the repository. Reusing the
        # old checkout recorded the old commit as provenance and hid the relocation.
        first = make_repo(self.tmp / "one", {"guide/old.md": "# Old\n\nbody text long enough\n"})
        write_config(self.cfg, first)
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)
        self.assertTrue((self.out / "demo" / "guide" / "old.md").is_file())

        second = make_repo(self.tmp / "two", {"guide/new.md": "# New\n\nbody text long enough\n"})
        write_config(self.cfg, second)
        self.assertEqual(run_build(self.cfg, self.work, self.out), 0)

        self.assertTrue((self.out / "demo" / "guide" / "new.md").is_file())
        self.assertFalse((self.out / "demo" / "guide" / "old.md").exists())
        entry = self.manifest()["sources"]["demo"]
        self.assertEqual(entry["repo"], second)

    def test_check_fails_on_a_ref_that_does_not_exist(self):
        repo = make_repo(self.tmp / "repo", {"a.md": "# A\n\nbody text long enough\n"})
        self.cfg.write_text(
            "version: 1\n"
            "sources:\n"
            "  demo:\n"
            "    kind: git\n"
            f"    repo: {repo}\n"
            "    ref: no-such-branch\n"
            "    include: [\"**/*.md\"]\n"
        )
        self.assertEqual(run_check(self.cfg), 1)

    def test_check_passes_on_a_good_ref(self):
        repo = make_repo(self.tmp / "repo", {"a.md": "# A\n\nbody text long enough\n"})
        write_config(self.cfg, repo)
        self.assertEqual(run_check(self.cfg), 0)


if __name__ == "__main__":
    unittest.main()
