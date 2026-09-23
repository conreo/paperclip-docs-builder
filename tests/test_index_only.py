"""
`--index-only`: rebuild the vector index without re-fetching the corpus.

Re-fetching every source to add one vector per page is the wrong price, and it is the
reason this mode exists. These tests drive the same stub as `test_embeddings`, and
assert the four things that decide whether an operator can rely on it: the index is
written in the shape the plugin reads, nothing is fetched, a killed run resumes, and
a failed run leaves the corpus alone.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch  # noqa: E402
from test_embeddings import DIM, StubEmbeddings, vector_for  # noqa: E402


def run_index_only(out: Path, endpoint: str, extra: list[str] | None = None) -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        return fetch.main(
            [
                "--index-only",
                "--out", str(out),
                "--embed-endpoint", endpoint,
                "--embed-model", "stub-model",
                "--embed-batch", "2",
                *(extra or []),
            ]
        )


def f32(values: list[float]) -> list[float]:
    """The matrix is float32, so an expected vector has to be rounded the same way."""
    packed = struct.pack(f"<{len(values)}f", *values)
    return list(struct.unpack(f"<{len(values)}f", packed))


class IndexOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import HTTPServer

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
        self.tmp = Path(TemporaryDirectory().name)
        self.corpus = self.tmp / "okf-bundles"
        # Two bundles, four concepts: a page with frontmatter, an index page, a page
        # with no frontmatter at all (the plugin counts it, so the index must too),
        # and a nested page.
        (self.corpus / "alpha").mkdir(parents=True)
        (self.corpus / "alpha" / "index.md").write_text("---\nokf_version: \"0.2\"\n---\n\n# Alpha\n")
        (self.corpus / "alpha" / "one.md").write_text(
            "---\ntitle: \"One\"\ndescription: \"First\"\ntags: [alpha, docs]\n---\n\nBody one.\n"
        )
        (self.corpus / "beta").mkdir(parents=True)
        (self.corpus / "beta" / "plain.md").write_text("# Plain\n\nNo frontmatter here.\n")

    def index_files(self) -> tuple[dict, bytes]:
        meta = json.loads((self.corpus / fetch.EMBEDDINGS_JSON).read_text())
        return meta, (self.corpus / fetch.EMBEDDINGS_BIN).read_bytes()

    def test_writes_an_index_the_plugin_can_read(self):
        self.assertEqual(run_index_only(self.corpus, self.endpoint), 0)

        meta, raw = self.index_files()
        self.assertEqual(meta["schema"], fetch.EMBEDDINGS_SCHEMA)
        self.assertEqual(meta["model"], "stub-model")
        self.assertEqual(meta["dim"], DIM)
        self.assertEqual(meta["count"], 3)
        self.assertTrue(meta["complete"])
        self.assertEqual(meta["bundles"], ["alpha", "beta"])
        self.assertEqual(
            meta["concept_ids"],
            ["alpha/index.md", "alpha/one.md", "beta/plain.md"],
        )
        # The shape the plugin asserts before reading a single float.
        self.assertEqual(len(raw), meta["count"] * meta["dim"] * 4)
        values = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        self.assertEqual(values[:DIM], f32(vector_for(fetch.concept_text(self.pages()[0]))))

    def test_the_journal_is_cleaned_up_on_success(self):
        run_index_only(self.corpus, self.endpoint)
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_JOURNAL).exists())
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_PARTIAL).exists())

    def test_fetches_nothing_and_ignores_the_source_list(self):
        # A sources file that does not exist, and a work directory that must never be
        # created: the whole point is that this mode cannot touch the network.
        work = self.tmp / "work"
        code = run_index_only(
            self.corpus,
            self.endpoint,
            ["--sources", str(self.tmp / "nope.yaml"), "--work", str(work)],
        )
        self.assertEqual(code, 0)
        self.assertFalse(work.exists())

    def test_resumes_from_the_journal(self):
        pages = self.pages()
        # A journal and a matrix as a killed run would leave them: the first concept
        # embedded, the rest not.
        (self.corpus / fetch.EMBEDDINGS_JOURNAL).write_text("alpha/index.md\n")
        (self.corpus / fetch.EMBEDDINGS_PARTIAL).write_bytes(struct.pack(f"<{DIM}f", *([0.5] * DIM)))

        self.assertEqual(run_index_only(self.corpus, self.endpoint), 0)

        sent = [text for request in StubEmbeddings.requests for text in request["input"]]
        embedded_texts = [fetch.concept_text(page) for page in pages]
        self.assertNotIn(embedded_texts[0], sent, "the journaled concept was embedded again")
        self.assertIn(embedded_texts[1], sent)

        meta, raw = self.index_files()
        self.assertEqual(meta["count"], len(pages))
        values = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        # The resumed vector survived, in the first row, untouched.
        self.assertEqual(values[:DIM], [0.5] * DIM)
        self.assertEqual(values[DIM : 2 * DIM], f32(vector_for(embedded_texts[1])))

    def test_a_journal_from_another_corpus_is_not_trusted(self):
        (self.corpus / fetch.EMBEDDINGS_JOURNAL).write_text("ghost/gone.md\n")
        (self.corpus / fetch.EMBEDDINGS_PARTIAL).write_bytes(struct.pack(f"<{DIM}f", *([0.5] * DIM)))

        self.assertEqual(run_index_only(self.corpus, self.endpoint), 0)

        meta, raw = self.index_files()
        self.assertEqual(meta["concept_ids"][0], "alpha/index.md")
        values = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        self.assertNotEqual(values[:DIM], [0.5] * DIM, "a foreign journal was resumed")

    def test_a_failed_endpoint_leaves_the_corpus_untouched(self):
        StubEmbeddings.fail_with = 500
        self.assertEqual(run_index_only(self.corpus, self.endpoint), 1)
        # No index half-written, and the corpus itself is still readable — a failed
        # index costs the index, never the documentation.
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_JSON).exists())
        self.assertFalse((self.corpus / fetch.EMBEDDINGS_BIN).exists())
        self.assertEqual(len(fetch.read_corpus_pages(self.corpus)), 3)

    def test_refuses_without_an_endpoint_or_a_model(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fetch.main(["--index-only", "--out", str(self.corpus)]), 2)
            self.assertEqual(
                fetch.main(
                    ["--index-only", "--out", str(self.corpus), "--embed-endpoint", self.endpoint]
                ),
                2,
            )

    def test_an_empty_corpus_is_an_error_not_an_empty_index(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(run_index_only(empty, self.endpoint), 1)
        self.assertFalse((empty / fetch.EMBEDDINGS_JSON).exists())

    def pages(self):
        return fetch.read_corpus_pages(self.corpus)


if __name__ == "__main__":
    unittest.main()
