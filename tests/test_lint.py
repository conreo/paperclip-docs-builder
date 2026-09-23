"""
Tests for the OKF linter.

Each case is a corpus built in a temporary directory, so every rule is exercised
against real files rather than a mocked file list — the linter's whole job is reading
bytes off a disk.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lint import ERROR, WARN, lint_corpus


def concept(body: str = "A page of documentation that is long enough to not be a stub.", **front: str) -> str:
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {value}")
    lines += ["---", "", body, ""]
    return "\n".join(lines)


class LintCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def codes(self, severity: str | None = None) -> list[str]:
        report = lint_corpus(self.root)
        self.report = report
        findings = report.findings if severity is None else [f for f in report.findings if f.severity == severity]
        return sorted(f.code for f in findings)


class TestConformantCorpus(LintCase):
    def test_a_minimal_concept_is_conformant(self) -> None:
        # §11: "a concept carrying just `type` is fully conformant". Nothing else may
        # be required, or the linter is stricter than the spec it claims to check.
        self.write("bundle/index.md", "# Bundle\n\n* [Thing](thing.md) - A thing worth documenting.\n")
        self.write("bundle/thing.md", concept(type='"Reference"'))
        report = lint_corpus(self.root)
        self.assertEqual([f.code for f in report.errors], [])
        self.assertEqual(report.concepts, 1)
        self.assertEqual(report.bundles, 1)

    def test_the_bundle_root_may_declare_the_version(self) -> None:
        # §8 and §12: the one place frontmatter is permitted in an index file.
        self.write(
            "bundle/index.md",
            '---\nokf_version: "0.2"\n---\n\n# Bundle\n\n* [Thing](thing.md) - A thing.\n',
        )
        self.write("bundle/thing.md", concept(type='"Reference"'))
        self.assertEqual([f.code for f in lint_corpus(self.root).errors], [])

    def test_html_inside_a_code_fence_is_not_a_finding(self) -> None:
        # A page that documents HTML must be allowed to show it: this is why the
        # builder has `outside_code_fences`, and the linter uses the same bracket.
        body = "Here is how the markup looks:\n\n```html\n<div class=\"x\">hello</div>\n```\n\nThat is all."
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Guide"', body=body))
        self.assertNotIn("hygiene/inline-html", self.codes())


class TestSpecViolations(LintCase):
    def test_type_is_required(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(title='"No type"'))
        self.assertIn("okf/type-missing", self.codes(ERROR))

    def test_an_index_may_not_carry_concept_frontmatter(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write(
            "bundle/sub/index.md",
            concept(type='"Reference"', title='"Landing page"', body="* [x](x.md)"),
        )
        self.write("bundle/sub/x.md", concept(type='"Reference"'))
        self.assertIn("okf/index-has-frontmatter", self.codes(ERROR))

    def test_okf_version_belongs_at_the_bundle_root_only(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', okf_version='"0.2"'))
        self.assertIn("okf/okf-version-misplaced", self.codes(ERROR))

    def test_generated_requires_an_actor(self) -> None:
        # §5.2: `generated.by` is REQUIRED within `generated`.
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", '---\ntype: "Reference"\ngenerated:\n  at: "2026-09-24T00:00:00Z"\n---\n\nBody long enough to pass the stub check.\n')
        self.assertIn("okf/generated-by-missing", self.codes(ERROR))

    def test_source_entries_require_a_resource(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write(
            "bundle/thing.md",
            '---\ntype: "Reference"\nsources:\n- title: "No resource here"\n---\n\nBody long enough to pass the stub check.\n',
        )
        self.assertIn("okf/sources-resource-missing", self.codes(ERROR))

    def test_timestamps_need_an_explicit_offset(self) -> None:
        # §5: "an ISO 8601 datetime with an explicit offset" — a bare local time makes
        # a corpus's freshness impossible to reason about.
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', timestamp="2026-09-24 10:00:00"))
        self.assertIn("okf/time-not-iso", self.codes(ERROR))

    def test_legacy_timestamp_is_a_warning_not_an_error(self) -> None:
        # §13.1 allows the fallback, so a v0.1 bundle stays consumable.
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', timestamp="2026-09-24T10:00:00Z"))
        self.assertIn("okf/timestamp-legacy", self.codes(WARN))
        self.assertNotIn("okf/timestamp-legacy", self.codes(ERROR))

    def test_unparseable_frontmatter_is_reported(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", '---\ntype: "Reference"\n  bad: [unclosed\n---\n\nA body.\n')
        self.assertIn("okf/frontmatter-unparseable", self.codes(ERROR))

    def test_a_reserved_filename_may_not_be_a_concept(self) -> None:
        # §3.1: `index.md` and `log.md` MUST NOT be used for concept documents.
        self.write("bundle/index.md", concept(type='"Reference"', title='"I am a concept"'))
        self.assertIn("structure/reserved-as-concept", self.codes(ERROR))


class TestHygiene(LintCase):
    def test_binary_read_as_text_is_an_error(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', body="A body with \ufffd in it, long enough to pass."))
        self.assertIn("hygiene/replacement-char", self.codes(ERROR))

    def test_inline_html_is_reported_because_it_is_indexed_as_prose(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', body="<div class=\"note\">Text long enough to pass the stub check.</div>"))
        self.assertIn("hygiene/inline-html", self.codes(WARN))

    def test_entities_and_anchor_artifacts(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', body="Redis &mdash; see [\\#](#anchor) for more detail here."))
        codes = self.codes(WARN)
        self.assertIn("hygiene/entity", codes)
        self.assertIn("hygiene/anchor-artifact", codes)

    def test_a_stub_body_is_reported(self) -> None:
        self.write("bundle/index.md", "# B\n")
        self.write("bundle/thing.md", concept(type='"Reference"', body="Too short."))
        self.assertIn("hygiene/short-body", self.codes(WARN))


class TestIndexQuality(LintCase):
    def test_entries_without_descriptions_are_reported(self) -> None:
        # §8: entries SHOULD include the linked concept's frontmatter description.
        self.write("bundle/index.md", "# B\n\n* [One](one.md)\n* [Two](two.md)\n")
        self.write("bundle/one.md", concept(type='"Reference"'))
        self.write("bundle/two.md", concept(type='"Reference"'))
        self.assertIn("index/no-descriptions", self.codes(WARN))

    def test_described_entries_pass(self) -> None:
        self.write("bundle/index.md", "# B\n\n* [One](one.md) - The first one.\n")
        self.write("bundle/one.md", concept(type='"Reference"'))
        self.assertNotIn("index/no-descriptions", self.codes(WARN))

    def test_a_bundle_without_an_index_is_reported(self) -> None:
        self.write("bundle/thing.md", concept(type='"Reference"'))
        self.assertIn("structure/no-index", self.codes(WARN))


class TestReportShape(LintCase):
    def test_the_summary_counts_what_it_scanned(self) -> None:
        self.write("a/index.md", "# A\n")
        self.write("a/one.md", concept(type='"Reference"'))
        self.write("b/index.md", "# B\n")
        self.write("b/two.md", concept(type='"Reference"'))
        report = lint_corpus(self.root)
        self.assertEqual((report.bundles, report.concepts, report.indexes), (2, 2, 2))

    def test_json_output_is_serialisable_and_grouped_by_code(self) -> None:
        self.write("b/index.md", "# B\n")
        self.write("b/one.md", concept(title='"No type"'))
        payload = lint_corpus(self.root).as_dict()
        self.assertEqual(payload["bundles"], 1)
        self.assertIn("okf/type-missing", payload["by_code"])
        self.assertEqual(payload["by_code"]["okf/type-missing"], 1)


if __name__ == "__main__":
    unittest.main()
