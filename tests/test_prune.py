"""
Removing a bundle: pages go, vectors go, and nothing is fetched or re-embedded.

The property under test is that a prune is a *deletion*, not a rebuild. Survivors'
vectors must be byte-identical afterwards — if they are not, the index was rebuilt
and the operator paid for an embedding run they did not ask for. And the operation
must be idempotent, because a request retried after succeeding is normal.
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
from test_embeddings import StubEmbeddings  # noqa: E402


class PruneTest(unittest.TestCase):
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
        self.corpus = Path(TemporaryDirectory().name) / "okf-bundles"
        for bundle, pages in (("keep", 3), ("drop", 2)):
            for index in range(pages):
                page = self.corpus / bundle / f"page-{index}.md"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text(
                    f"---\ntitle: \"{bundle} {index}\"\ntype: doc\n---\n\n"
                    f"Body text for {bundle} page {index}, long enough to embed.\n",
                    encoding="utf-8",
                )
        self.index()

    def index(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = fetch.main([
                "--index-only", "--out", str(self.corpus),
                "--embed-endpoint", self.endpoint, "--embed-model", "stub-model",
            ])
        self.assertEqual(code, 0)

    def prune(self, *bundles: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return fetch.main(["--prune", *bundles, "--out", str(self.corpus)])

    def meta(self) -> dict:
        return json.loads((self.corpus / fetch.EMBEDDINGS_JSON).read_text())

    def test_removing_a_bundle_takes_its_pages_and_only_its_vectors(self):
        before = self.meta()
        before_ids = list(before["concept_ids"])
        before_raw = (self.corpus / fetch.EMBEDDINGS_BIN).read_bytes()
        row = before["dim"] * 4
        survivors = {cid: before_raw[i * row:(i + 1) * row] for i, cid in enumerate(before_ids)
                     if cid.startswith("keep/")}

        self.assertEqual(self.prune("drop"), 0)

        self.assertFalse((self.corpus / "drop").exists())
        self.assertTrue((self.corpus / "keep" / "page-0.md").is_file())

        after = self.meta()
        self.assertEqual(after["count"], 3)
        self.assertTrue(all(cid.startswith("keep/") for cid in after["concept_ids"]))
        self.assertEqual(after["bundles"], ["keep"])
        # The dimension and the model are properties of the vectors that remain.
        self.assertEqual(after["dim"], before["dim"])
        self.assertEqual(after["model"], before["model"])

        after_raw = (self.corpus / fetch.EMBEDDINGS_BIN).read_bytes()
        self.assertEqual(len(after_raw), after["count"] * row)
        for position, cid in enumerate(after["concept_ids"]):
            self.assertEqual(
                after_raw[position * row:(position + 1) * row], survivors[cid],
                f"{cid} was re-embedded rather than kept",
            )

        # Nothing was re-embedded: the stub server saw only the original build.
        self.assertEqual(len(StubEmbeddings.requests), 1)

    def test_a_prune_without_an_index_still_removes_the_pages(self):
        (self.corpus / fetch.EMBEDDINGS_JSON).unlink()
        (self.corpus / fetch.EMBEDDINGS_BIN).unlink()
        self.assertEqual(self.prune("drop"), 0)
        self.assertFalse((self.corpus / "drop").exists())

    def test_pruning_twice_is_not_an_error(self):
        # A request retried after it succeeded must not look like a failure.
        self.assertEqual(self.prune("drop"), 0)
        self.assertEqual(self.prune("drop"), 0)
        self.assertFalse((self.corpus / "drop").exists())

    def test_it_refuses_a_path_instead_of_a_bundle_name(self):
        for hostile in ("../keep", "keep/../drop", "..", "/etc"):
            self.assertEqual(self.prune(hostile), 1, hostile)
        self.assertTrue((self.corpus / "keep").exists())
        self.assertTrue((self.corpus / "drop").exists())

    def test_the_manifest_stops_claiming_what_was_removed(self):
        (self.corpus / fetch.MANIFEST_FILENAME).write_text(json.dumps({
            "sources": {"keep": {"repo": "x"}, "drop": {"repo": "y"}},
            "totals": {"pages": 5, "bundles": 2},
        }))
        self.assertEqual(self.prune("drop"), 0)

        manifest = json.loads((self.corpus / fetch.MANIFEST_FILENAME).read_text())
        self.assertEqual(list(manifest["sources"]), ["keep"])
        self.assertEqual(manifest["totals"]["bundles"], 1)
        self.assertEqual(manifest["totals"]["pages"], 2 + 1)  # 2 dropped from the 5 counted
        self.assertEqual(manifest["pruned"][0]["bundle"], "drop")
        self.assertEqual(manifest["pruned"][0]["pages"], 2)
        self.assertEqual(manifest["pruned"][0]["vectors"], 2)
        self.assertIn("at", manifest["pruned"][0])

    def test_removing_two_bundles_at_once_empties_the_index(self):
        for bundle in ("keep", "drop"):
            self.assertEqual(self.prune(bundle), 0)
        self.assertEqual(self.meta()["count"], 0)
        self.assertEqual((self.corpus / fetch.EMBEDDINGS_BIN).read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
