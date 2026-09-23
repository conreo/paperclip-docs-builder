"""
Runner and local-source tests.

The runner is the half that actually writes the corpus, so these cover the paths a
cron job will meet at three in the morning: a request it does not understand, a
request already satisfied, a request whose sources have gone, and a plain ordinary
build. All hermetic — local git repositories and local folders, no network.
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
import runner  # noqa: E402

USER = ["-c", "user.email=test@example.invalid", "-c", "user.name=Test"]


def make_repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *USER, "commit", "-q", "-m", "docs"], cwd=root, check=True)
    return str(root)


def request_body(corpus_root: Path, sources: list[dict], requested_at: str) -> dict:
    return {
        "schema": 1,
        "requestedAt": requested_at,
        "reason": "test",
        "corpusRoot": str(corpus_root),
        "sources": sources,
    }


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.requests = self.tmp / "requests"
        self.requests.mkdir()
        self.corpus = self.tmp / "corpus" / "okf-bundles"
        self.repo = make_repo(self.tmp / "repo", {"guide/intro.md": "# Intro\n\nbody text long enough\n"})

    def write_request(self, body: dict) -> None:
        (self.requests / runner.REQUEST_FILENAME).write_text(json.dumps(body))

    def response(self) -> dict:
        return json.loads((self.requests / runner.RESPONSE_FILENAME).read_text())

    def run_once(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.main(["--requests", str(self.requests), "--out", str(self.corpus), "--quiet"])

    def test_an_ordinary_request_builds_and_reports(self):
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "demo", "kind": "git", "repo": self.repo, "include": ["**/*.md"]}],
                "2026-09-23T00:00:00Z",
            )
        )
        self.assertEqual(self.run_once(), 0)

        self.assertTrue((self.corpus / "demo" / "guide" / "intro.md").is_file())
        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertEqual(response["pages"], 1)
        # The request is cleared, so the next tick does not build it again.
        self.assertFalse((self.requests / runner.REQUEST_FILENAME).exists())
        self.assertTrue((self.requests / (runner.REQUEST_FILENAME + runner.DONE_SUFFIX)).exists())

    def test_no_request_is_not_an_error(self):
        # Cron's exit code should mean "something needs attention", not "nothing to do".
        self.assertEqual(self.run_once(), 0)
        self.assertFalse((self.requests / runner.RESPONSE_FILENAME).exists())

    def test_a_schema_it_does_not_understand_is_refused_loudly(self):
        # A newer plugin must not be silently ignored: that looks exactly like a
        # build that found nothing to do.
        self.write_request(
            request_body(self.corpus, [{"id": "d", "kind": "git", "repo": self.repo}], "2026-09-23T00:00:00Z")
            | {"schema": 99}
        )
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.response()["status"], "refused")
        self.assertIn("schema", self.response()["reason"])
        self.assertTrue((self.requests / (runner.REQUEST_FILENAME + runner.FAILED_SUFFIX)).exists())

    def test_an_unknown_source_kind_is_refused_by_name(self):
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "d", "kind": "telepathy", "repo": "x"}],
                "2026-09-23T00:00:00Z",
            )
        )
        self.assertEqual(self.run_once(), 1)
        self.assertIn("telepathy", self.response()["reason"])

    def test_a_request_with_no_sources_is_refused(self):
        self.write_request(request_body(self.corpus, [], "2026-09-23T00:00:00Z"))
        self.assertEqual(self.run_once(), 1)
        self.assertIn("no sources", self.response()["reason"])

    def test_corrupt_json_is_quarantined_rather_than_retried_forever(self):
        (self.requests / runner.REQUEST_FILENAME).write_text("{not json")
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.response()["status"], "refused")
        self.assertFalse((self.requests / runner.REQUEST_FILENAME).exists())

    def test_an_already_satisfied_request_is_skipped(self):
        # A cron and the settings button can race. Skipping makes honouring a
        # request idempotent, which is what makes a retry safe.
        sources = [{"id": "demo", "kind": "git", "repo": self.repo, "include": ["**/*.md"]}]
        self.write_request(request_body(self.corpus, sources, "2026-09-23T00:00:00Z"))
        self.assertEqual(self.run_once(), 0)
        built_at = json.loads((self.corpus / fetch.MANIFEST_FILENAME).read_text())["built_at"]

        self.write_request(request_body(self.corpus, sources, built_at))
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.response()["status"], "skipped")
        self.assertIn("already built", self.response()["reason"])

    def test_a_source_that_cannot_be_fetched_reports_failure(self):
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "gone", "kind": "git", "repo": str(self.tmp / "no-such-repo")}],
                "2026-09-23T00:00:00Z",
            )
        )
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.response()["status"], "failed")


class LocalSourceTest(unittest.TestCase):
    """`kind: local` — a project's own documentation, folded in as a bundle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.docs = self.tmp / "docs"
        (self.docs / "guide").mkdir(parents=True)
        (self.docs / "hello.md").write_text("# Hello\n\nA local page with enough text to keep.\n")
        (self.docs / "guide" / "deeper.md").write_text("# Deeper\n\nAnother local page.\n")
        self.out = self.tmp / "out" / "okf-bundles"
        self.cfg = self.tmp / "sources.yaml"

    def build(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return fetch.main(
                [
                    "--sources", str(self.cfg),
                    "--out", str(self.out),
                    "--work", str(self.tmp / "work"),
                    "--local-root", str(self.tmp),
                    "--no-previous",
                    "--jobs", "1",
                ]
            )

    def test_a_relative_folder_builds_as_a_bundle(self):
        self.cfg.write_text(
            "version: 1\nsources:\n  handbook:\n    kind: local\n    title: Handbook\n    folder: docs\n"
        )
        self.assertEqual(self.build(), 0)
        self.assertTrue((self.out / "handbook" / "hello.md").is_file())
        self.assertTrue((self.out / "handbook" / "guide" / "deeper.md").is_file())
        manifest = json.loads((self.out / fetch.MANIFEST_FILENAME).read_text())
        self.assertEqual(manifest["sources"]["handbook"]["pages"], 2)
        # A folder's own mtime is its snapshot date, not the build time.
        self.assertTrue(manifest["sources"]["handbook"]["commit_date"])

    def test_a_missing_folder_fails_with_a_usable_message(self):
        self.cfg.write_text("version: 1\nsources:\n  handbook:\n    kind: local\n    folder: nope\n")
        self.assertEqual(self.build(), 1)

    def test_check_reports_a_local_source(self):
        self.cfg.write_text("version: 1\nsources:\n  handbook:\n    kind: local\n    folder: docs\n")
        with contextlib.redirect_stdout(io.StringIO()):
            code = fetch.main(
                ["--sources", str(self.cfg), "--check", "--local-root", str(self.tmp)]
            )
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
