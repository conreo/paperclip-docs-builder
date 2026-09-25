"""
Requests that carry an embed block, and the `index` mode.

A corpus rebuild and an index rebuild are separable operations now, and the point of
separating them is that the second must not need the first: no sources, no fetch, no
registry. These tests hold that line, and pin the default that keeps an old plugin's
requests meaning what they always did.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch  # noqa: E402
import runner  # noqa: E402
from test_embeddings import StubEmbeddings  # noqa: E402
from test_runner import make_repo, request_body  # noqa: E402


class RunnerIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubEmbeddings)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/v1/embeddings"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubEmbeddings.requests = []
        StubEmbeddings.fail_with = None
        self.tmp = Path(TemporaryDirectory().name)
        self.requests = self.tmp / "requests"
        self.requests.mkdir(parents=True)
        self.corpus = self.tmp / "corpus" / "okf-bundles"
        # A corpus already on disk, as a previous build would have left it.
        (self.corpus / "handbook").mkdir(parents=True)
        (self.corpus / "handbook" / "intro.md").write_text(
            "---\ntitle: \"Intro\"\ntags: [handbook]\n---\n\nSome documentation.\n"
        )
        (self.corpus / "handbook" / "more.md").write_text(
            "---\ntitle: \"More\"\n---\n\nMore documentation.\n"
        )
        self.repo = make_repo(self.tmp / "repo", {"guide/intro.md": "# Intro\n\nbody text long enough\n"})

    def write_request(self, body: dict) -> None:
        (self.requests / runner.REQUEST_FILENAME).write_text(json.dumps(body))

    def response(self) -> dict:
        return json.loads((self.requests / runner.RESPONSE_FILENAME).read_text())

    def run_once(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.main(
                ["--requests", str(self.requests), "--out", str(self.corpus), "--quiet", *extra]
            )

    def embed_block(self) -> dict:
        return {"endpoint": self.endpoint, "model": "stub-model"}

    def test_an_index_request_needs_no_sources(self):
        # No `sources` key at all: the registry is not consulted in this mode, so a
        # corpus whose sources have gone can still be indexed.
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": str(self.corpus),
                "mode": "index",
                "embed": self.embed_block(),
            }
        )
        self.assertEqual(self.run_once(), 0)

        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertEqual(response["reason"], "index rebuilt")
        self.assertEqual(response["index"]["count"], 2)
        self.assertEqual(response["index"]["model"], "stub-model")
        self.assertTrue((self.corpus / fetch.EMBEDDINGS_JSON).is_file())
        self.assertTrue((self.corpus / fetch.EMBEDDINGS_BIN).is_file())
        self.assertFalse((self.requests / runner.REQUEST_FILENAME).exists())
        self.assertTrue((self.requests / (runner.REQUEST_FILENAME + runner.DONE_SUFFIX)).exists())

    def test_an_index_request_without_an_embed_block_is_refused_by_name(self):
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": str(self.corpus),
                "mode": "index",
            }
        )
        self.assertEqual(self.run_once(), 1)

        response = self.response()
        self.assertEqual(response["status"], "refused")
        self.assertIn("embed", response["reason"])
        self.assertTrue((self.requests / (runner.REQUEST_FILENAME + runner.FAILED_SUFFIX)).exists())

    def test_an_embed_block_with_no_mode_builds_both(self):
        # The shape the plugin sends once RAG is on: corpus and index in one pass.
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "demo", "kind": "git", "repo": self.repo, "include": ["**/*.md"]}],
                "2026-09-23T00:00:00Z",
            )
            | {"embed": self.embed_block()}
        )
        self.assertEqual(self.run_once(), 0)

        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertEqual(response["index"]["count"], 1)
        self.assertTrue((self.corpus / "demo" / "guide" / "intro.md").is_file())
        self.assertTrue((self.corpus / fetch.EMBEDDINGS_JSON).is_file())

    def test_mode_okf_ignores_an_embed_block(self):
        # An explicit `okf` is a request for the corpus only; embedding anyway would
        # make the mode meaningless.
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "demo", "kind": "git", "repo": self.repo, "include": ["**/*.md"]}],
                "2026-09-23T00:00:00Z",
            )
            | {"mode": "okf", "embed": self.embed_block()}
        )
        self.assertEqual(self.run_once(), 0)

        self.assertEqual(self.response()["status"], "built")
        self.assertTrue((self.corpus / "demo" / "guide" / "intro.md").is_file())
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_JSON).exists())

    def test_a_mode_it_does_not_understand_is_refused_by_name(self):
        self.write_request(
            request_body(
                self.corpus,
                [{"id": "demo", "kind": "git", "repo": self.repo, "include": ["**/*.md"]}],
                "2026-09-23T00:00:00Z",
            )
            | {"mode": "everything"}
        )
        self.assertEqual(self.run_once(), 1)

        response = self.response()
        self.assertEqual(response["status"], "refused")
        self.assertIn("everything", response["reason"])
        self.assertIn("index", response["reason"])

    def test_a_path_the_host_cannot_see_is_refused_not_guessed_at(self):
        # This used to fall back to the runner's own --out. With one tenant that was a
        # convenience; with two it is how a request from one organization deletes
        # another's pages. The runner no longer guesses.
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": "/paperclip/offline-docs/okf-bundles",
                "mode": "index",
                "embed": self.embed_block(),
            }
        )
        self.assertEqual(self.run_once(), 1)

        response = self.response()
        self.assertEqual(response["status"], "refused")
        self.assertIn("not configured to serve", response["reason"])
        # And nothing was written into the corpus it would have guessed at.
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_JSON).exists())
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_BIN).exists())

    def test_a_declared_path_is_served_when_the_operator_maps_it(self):
        # The container/host namespace difference, solved by declaration rather than by
        # a fallback: the operator says which path means which.
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": "/paperclip/offline-docs/okf-bundles",
                "mode": "index",
                "embed": self.embed_block(),
            }
        )
        self.assertEqual(
            self.run_once("--map", f"/paperclip/offline-docs/okf-bundles={self.corpus}"), 0
        )

        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertEqual(response["corpus_root"], str(self.corpus))
        self.assertIn("maps to", response["reason"])
        self.assertTrue((self.corpus / fetch.EMBEDDINGS_JSON).is_file())

    def test_another_corpus_the_runner_was_not_told_about_is_refused(self):
        # It exists, it is a directory, and it is still not this runner's business.
        other = self.tmp / "someone-elses"
        (other / "bundle").mkdir(parents=True)
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": str(other),
                "mode": "prune",
                "sources": [],
                "remove": {"bundles": ["bundle"]},
            }
        )
        self.assertEqual(self.run_once(), 1)
        self.assertIn("not configured to serve", self.response()["reason"])
        self.assertTrue((other / "bundle").is_dir(), "another corpus was deleted")

    def test_a_second_corpus_is_served_when_it_is_listed(self):
        other = self.tmp / "second"
        (other / "bundle").mkdir(parents=True)
        (other / "bundle" / "a.md").write_text("---\ntitle: a\n---\n\nbody\n")
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": str(other),
                "mode": "prune",
                "sources": [],
                "remove": {"bundles": ["bundle"]},
            }
        )
        self.assertEqual(self.run_once("--corpus", str(other)), 0)
        self.assertEqual(self.response()["status"], "built")
        self.assertFalse((other / "bundle").exists())

    def test_a_path_the_host_can_see_is_still_honoured(self):
        self.write_request(
            {
                "schema": 1,
                "requestedAt": "2026-09-23T00:00:00Z",
                "reason": "test",
                "corpusRoot": str(self.corpus),
                "mode": "index",
                "embed": self.embed_block(),
            }
        )
        self.assertEqual(self.run_once(), 0)

        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertNotIn("cannot see", response["reason"])


if __name__ == "__main__":
    unittest.main()


class RunnerPruneTest(unittest.TestCase):
    """`mode: prune` — a removal the runner has to get right on its own."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubEmbeddings)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/v1/embeddings"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubEmbeddings.requests = []
        self.tmp = Path(TemporaryDirectory().name)
        self.requests = self.tmp / "requests"
        self.requests.mkdir(parents=True)
        self.corpus = self.tmp / "corpus" / "okf-bundles"
        for bundle in ("handbook", "legacy"):
            page = self.corpus / bundle / "intro.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(f"---\ntitle: \"{bundle}\"\n---\n\nBody of {bundle}.\n")
        with contextlib.redirect_stdout(io.StringIO()):
            fetch.main([
                "--index-only", "--out", str(self.corpus),
                "--embed-endpoint", self.endpoint, "--embed-model", "stub-model",
            ])

    def write_request(self, body: dict) -> None:
        (self.requests / runner.REQUEST_FILENAME).write_text(json.dumps(body))

    def prune_request(self, bundles, mode="prune"):
        self.write_request({
            "schema": 1,
            "requestedAt": "2026-09-23T00:00:00Z",
            "reason": "test",
            "corpusRoot": str(self.corpus),
            "mode": mode,
            "sources": [],
            "remove": {"bundles": bundles},
        })

    def run_once(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.main(
                ["--requests", str(self.requests), "--out", str(self.corpus), "--quiet", *extra]
            )

    def response(self) -> dict:
        return json.loads((self.requests / runner.RESPONSE_FILENAME).read_text())

    def test_a_prune_reports_what_it_removed_and_needs_no_sources(self):
        self.prune_request(["legacy"])
        self.assertEqual(self.run_once(), 0)

        response = self.response()
        self.assertEqual(response["status"], "built")
        self.assertIn("removed 1 bundle", response["reason"])
        self.assertEqual(response["removed"][0]["bundle"], "legacy")
        self.assertEqual(response["removed"][0]["pages"], 1)
        self.assertEqual(response["removed"][0]["vectors"], 1)
        self.assertFalse((self.corpus / "legacy").exists())
        self.assertTrue((self.corpus / "handbook").exists())
        self.assertTrue((self.requests / (runner.REQUEST_FILENAME + runner.DONE_SUFFIX)).exists())

    def test_a_prune_with_no_bundles_is_refused_by_name(self):
        self.prune_request([])
        self.assertEqual(self.run_once(), 1)
        response = self.response()
        self.assertEqual(response["status"], "refused")
        self.assertIn("remove.bundles", response["reason"])

    def test_a_prune_naming_a_path_is_refused(self):
        # `../handbook` would otherwise be a directory traversal dressed as a bundle.
        self.prune_request(["../handbook"])
        self.assertEqual(self.run_once(), 1)
        self.assertIn("not a bundle name", self.response()["reason"])
        self.assertTrue((self.corpus / "handbook").exists())

    def test_prune_does_not_skip_a_recent_corpus(self):
        # A deletion must never be skipped because the corpus looks freshly built.
        self.prune_request(["legacy"])
        self.assertEqual(self.run_once(), 0)
        self.prune_request(["handbook"])
        self.assertEqual(self.run_once(), 0)
        self.assertFalse((self.corpus / "handbook").exists())
