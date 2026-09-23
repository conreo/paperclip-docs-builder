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

    def run_once(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.main(["--requests", str(self.requests), "--out", str(self.corpus), "--quiet"])

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


if __name__ == "__main__":
    unittest.main()
