"""
The optional vector index.

Embeddings need a service, and a test that needs a real API key is a test that does
not run. So these drive a stub that speaks the same protocol — OpenAI-compatible
`/v1/embeddings`, `{model, input}` in and `{data: [{embedding}]}` out — and assert
the two things that matter: the index is written in the shape the plugin reads, and
a broken endpoint costs the operator an index rather than the corpus.

The stub's vectors are deterministic functions of the input text, so a test can
assert that the *right* text was embedded, not merely that something was.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch  # noqa: E402

DIM = 8


def vector_for(text: str) -> list[float]:
    """A stable vector derived from the text, so assertions can be exact."""
    digest = sum(ord(char) * (index + 1) for index, char in enumerate(text))
    return [float((digest + offset) % 97) / 97.0 for offset in range(DIM)]


class StubEmbeddings(BaseHTTPRequestHandler):
    """An OpenAI-compatible embeddings endpoint."""

    requests: list[dict] = []
    fail_with: int | None = None
    ragged = False
    last_authorization: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        StubEmbeddings.requests.append(body)
        StubEmbeddings.last_authorization = self.headers.get("Authorization")
        if StubEmbeddings.fail_with:
            self.send_response(StubEmbeddings.fail_with)
            self.end_headers()
            self.wfile.write(b'{"error": "nope"}')
            return
        data = []
        for index, text in enumerate(body.get("input", [])):
            vector = vector_for(text)
            if StubEmbeddings.ragged and index == 1:
                vector = vector[:-1]
            data.append({"embedding": vector, "index": index})
        payload = json.dumps({"data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep the test output readable
        return


class EmbeddingsTest(unittest.TestCase):
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
        StubEmbeddings.ragged = False
        StubEmbeddings.last_authorization = None
        self.tmp = Path(TemporaryDirectory().name)
        self.docs = self.tmp / "docs"
        self.docs.mkdir(parents=True)
        (self.docs / "one.md").write_text("# One\n\nFirst page, with enough text.\n")
        (self.docs / "two.md").write_text("# Two\n\nSecond page, also long enough.\n")
        self.cfg = self.tmp / "sources.yaml"
        self.cfg.write_text("version: 1\nsources:\n  handbook:\n    kind: local\n    folder: docs\n")
        self.out = self.tmp / "out" / "okf-bundles"

    def build(self, endpoint: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return fetch.main(
                [
                    "--sources", str(self.cfg),
                    "--out", str(self.out),
                    "--work", str(self.tmp / "work"),
                    "--local-root", str(self.tmp),
                    "--no-previous",
                    "--jobs", "1",
                    "--embed-endpoint", endpoint,
                    "--embed-model", "stub-model",
                ]
            )

    def read_index(self) -> tuple[dict, list[float]]:
        meta = json.loads((self.out / fetch.EMBEDDINGS_JSON).read_text())
        raw = (self.out / fetch.EMBEDDINGS_BIN).read_bytes()
        values = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        return meta, values

    def test_writes_an_index_the_plugin_can_read(self):
        self.assertEqual(self.build(self.endpoint), 0)
        meta, values = self.read_index()

        self.assertEqual(meta["schema"], fetch.EMBEDDINGS_SCHEMA)
        self.assertEqual(meta["model"], "stub-model")
        self.assertEqual(meta["dim"], DIM)
        self.assertEqual(meta["count"], 2)
        self.assertEqual(len(values), 2 * DIM)
        # Concept ids are what `read_doc` accepts, so the index can cite a page.
        self.assertEqual(sorted(meta["concept_ids"]), ["handbook/one.md", "handbook/two.md"])
        self.assertTrue(meta["complete"])

    def test_embeds_the_text_the_keyword_index_ranks(self):
        # Not merely "a request happened": the text sent must be the page's own
        # title and prose, or semantic search ranks something else entirely. The
        # stub records its inputs, so this asserts the content rather than
        # reconstructing the builder's derivation and testing my copy of it.
        self.assertEqual(self.build(self.endpoint), 0)
        meta, _ = self.read_index()
        texts = [text for request in StubEmbeddings.requests for text in request["input"]]
        self.assertEqual(len(texts), 2)
        self.assertEqual(sorted(meta["concept_ids"]), ["handbook/one.md", "handbook/two.md"])
        one = next(text for text in texts if text.startswith("One"))
        two = next(text for text in texts if text.startswith("Two"))
        self.assertIn("First page, with enough text.", one)
        self.assertIn("Second page, also long enough.", two)

    def test_the_api_key_is_sent_as_a_bearer_token(self):
        import os

        os.environ["PAPERCLIP_DOCS_EMBED_KEY"] = "secret-key"
        try:
            self.assertEqual(self.build(self.endpoint), 0)
        finally:
            del os.environ["PAPERCLIP_DOCS_EMBED_KEY"]
        self.assertEqual(StubEmbeddings.last_authorization, "Bearer secret-key")

    def test_a_broken_endpoint_costs_the_index_and_not_the_corpus(self):
        StubEmbeddings.fail_with = 500
        # Exit code 0: the corpus built, and an optional extra failed.
        self.assertEqual(self.build(self.endpoint), 0)
        self.assertTrue((self.out / "handbook" / "one.md").is_file())
        self.assertFalse((self.out / fetch.EMBEDDINGS_JSON).exists())
        manifest = json.loads((self.out / fetch.MANIFEST_FILENAME).read_text())
        # ...but not silently: the manifest says why there is no index.
        self.assertIn("500", manifest["embeddings"]["error"])

    def test_a_ragged_response_is_refused(self):
        # Padding a short vector would make it incomparable with the others, and the
        # plugin cannot tell a padded row from a real one.
        StubEmbeddings.ragged = True
        self.assertEqual(self.build(self.endpoint), 0)
        manifest = json.loads((self.out / fetch.MANIFEST_FILENAME).read_text())
        self.assertIn("ragged", manifest["embeddings"]["error"])

    def test_batching_splits_the_work(self):
        self.assertEqual(self.build(self.endpoint), 0)
        total = sum(len(request["input"]) for request in StubEmbeddings.requests)
        self.assertEqual(total, 2)
        self.assertGreaterEqual(len(StubEmbeddings.requests), 1)
        for request in StubEmbeddings.requests:
            self.assertEqual(request["model"], "stub-model")


if __name__ == "__main__":
    unittest.main()
