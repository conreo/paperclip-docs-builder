#!/usr/bin/env python3
"""
Check an OKF corpus against the published specification.

## Why this is a program and not a prompt

Conformance is a property of files, so deciding it needs no model: every rule below is
a comparison against text. Asking an LLM "is this bundle valid OKF?" costs tokens per
run, gives a different answer each time, and cannot be wired into a build. This can be
run on every rebuild for nothing, in CI, or by an agent as a single tool call whose
output is the same every time.

The rules are the specification's, quoted by section in the code, plus a small set of
hygiene checks that are not in the spec but that we have actually been bitten by
(markup ingested as prose, entities left undecoded, binaries read as text).

    python3 lint.py out/okf-bundles            # human-readable
    python3 lint.py out/okf-bundles --json      # machine-readable
    python3 lint.py out/okf-bundles --strict    # warnings fail too

Spec: GoogleCloudPlatform/knowledge-catalog/okf, SPEC.md, version 0.2.
Exit code is 1 when errors are found, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from fetch import CODE_FENCE, outside_code_fences, strip_frontmatter

#: §3.1 — "have defined meaning at any level of the hierarchy and MUST NOT be used
#: for concept documents".
RESERVED_INDEX = "index.md"
RESERVED_LOG = "log.md"

ERROR = "error"
WARN = "warn"
INFO = "info"

#: §12 — the only place frontmatter is permitted inside an index file.
VERSION_KEY = "okf_version"

#: §11 — "a concept carrying just `type` is fully conformant". Everything else below
#: is a SHOULD or a hygiene rule, never a reason to reject a concept.
TIMESTAMP_KEYS = ("generated.at", "timestamp", "stale_after")

# §5.2 requires an explicit offset on every timestamp: a bare local time is ambiguous,
# and ambiguity is what makes a corpus's freshness impossible to reason about.
ISO_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")

TAG = re.compile(r"</?(?:html|head|body|div|span|table|thead|tbody|tr|td|th|script|style|nav|aside|footer|header|section|article|iframe|svg|img|a|p|ul|ol|li|br|hr|code|pre)\b", re.I)
ENTITY = re.compile(r"&(?:[a-zA-Z]{2,10}|#\d{1,6}|#x[0-9a-fA-F]{2,6});")
ANCHOR_ARTIFACT = re.compile(r"\[\\#\]\(#[^)]*\)")
REPLACEMENT = "\ufffd"
MOJIBAKE = ("Â", "â€")
INDEX_ENTRY = re.compile(r"^\s*[*-]\s+\[(?P<title>[^\]]*)\]\((?P<url>[^)]*)\)\s*(?P<rest>.*)$")

# A short body is not an error — but a page of forty characters is almost always a
# stub that will never answer anything, and it still costs a concept id and a listing.
MIN_BODY_CHARS = 40

# `read_doc` caps a body at 40,000 characters by default; a page beyond that is
# truncated for every agent that reads it, so it is worth knowing about.
LARGE_PAGE_CHARS = 40_000


@dataclass
class Finding:
    severity: str
    code: str
    path: str
    message: str


@dataclass
class LintReport:
    root: str
    bundles: int = 0
    concepts: int = 0
    indexes: int = 0
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, code: str, path: str, message: str) -> None:
        self.findings.append(Finding(severity, code, path, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == INFO]

    def as_dict(self) -> dict:
        by_code: dict[str, int] = {}
        for finding in self.findings:
            by_code[finding.code] = by_code.get(finding.code, 0) + 1
        return {
            "root": self.root,
            "bundles": self.bundles,
            "concepts": self.concepts,
            "indexes": self.indexes,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
            "findings": [
                {"severity": f.severity, "code": f.code, "path": f.path, "message": f.message}
                for f in self.findings
            ],
        }


def is_iso_timestamp(value: object) -> bool:
    """§5: "an ISO 8601 datetime with an explicit offset"."""
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str):
        return False
    return bool(ISO_OFFSET.match(value.strip()))


def check_frontmatter(report: LintReport, rel: str, front: dict, body: str, is_index: bool, is_root_index: bool) -> None:
    """The rules a concept's frontmatter must satisfy."""
    # §3.1 comes first, and applies to index.md as well: a reserved filename must never
    # be a concept document. Checked before the index branch below, which returns early
    # — an index.md carrying `type` is precisely the case that branch was hiding.
    name = Path(rel).name
    if name in (RESERVED_INDEX, RESERVED_LOG) and front.get("type"):
        report.add(
            ERROR,
            "structure/reserved-as-concept",
            rel,
            f"`{name}` is reserved and MUST NOT be a concept document (§3.1); it carries type "
            f"{front.get('type')!r}",
        )

    if is_index:
        # §8 — "Index files contain no frontmatter, with one exception: a bundle-root
        # `index.md` MAY carry an `okf_version` key".
        if front:
            extra = {k: v for k, v in front.items() if k != VERSION_KEY}
            if extra:
                report.add(
                    ERROR,
                    "okf/index-has-frontmatter",
                    rel,
                    "index files carry no frontmatter (§8); found "
                    + ", ".join(sorted(extra))
                    + ". A source's own landing page belongs in a concept document, and "
                    "the index beside it is a listing.",
                )
            if VERSION_KEY in front and not is_root_index:
                report.add(
                    ERROR,
                    "okf/okf-version-misplaced",
                    rel,
                    "`okf_version` is only valid in a bundle-root index.md (§12)",
                )
        return

    if VERSION_KEY in front:
        report.add(
            ERROR,
            "okf/okf-version-misplaced",
            rel,
            "`okf_version` belongs in the bundle-root index.md, not in a concept (§12)",
        )

    # §4.1 — `type` is the only always-required key.
    concept_type = front.get("type")
    if not (isinstance(concept_type, str) and concept_type.strip()):
        report.add(ERROR, "okf/type-missing", rel, "a concept document requires a non-empty `type` (§4.1)")

    # §5.2 — `generated.by` is REQUIRED within `generated`.
    generated = front.get("generated")
    if isinstance(generated, dict):
        by = generated.get("by")
        if not (isinstance(by, str) and by.strip()):
            report.add(ERROR, "okf/generated-by-missing", rel, "`generated.by` is required when `generated` is present (§5.2)")
        if "at" in generated and not is_iso_timestamp(generated["at"]):
            report.add(ERROR, "okf/time-not-iso", rel, f"`generated.at` is not ISO 8601 with an offset: {generated['at']!r}")
    elif generated is not None:
        report.add(ERROR, "okf/generated-shape", rel, "`generated` must be a mapping with `by` and `at` (§5.2)")

    # §5.1 — within a `sources` entry, `resource` is REQUIRED.
    sources = front.get("sources")
    if sources is not None:
        entries = sources if isinstance(sources, list) else [sources]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                report.add(ERROR, "okf/sources-shape", rel, f"`sources[{index}]` is not a mapping (§5.1)")
                continue
            resource = entry.get("resource")
            if not (isinstance(resource, str) and resource.strip()):
                report.add(ERROR, "okf/sources-resource-missing", rel, f"`sources[{index}].resource` is required (§5.1)")

    # §13.1 — `timestamp` is superseded by `generated.at`.
    if "timestamp" in front and not (isinstance(generated, dict) and "at" in generated):
        report.add(
            WARN,
            "okf/timestamp-legacy",
            rel,
            "`timestamp` is superseded by `generated.at` (§13.1); consumers may fall back, "
            "so this is consumable but not current",
        )
    for key in ("timestamp", "stale_after"):
        if key in front and not is_iso_timestamp(front[key]):
            report.add(ERROR, "okf/time-not-iso", rel, f"`{key}` is not ISO 8601 with an offset: {front[key]!r}")

    if "stale_after" in front:
        # §5.5 — the point of an absolute instant is a plain comparison, so it has to
        # be comparable.
        pass


def check_body(report: LintReport, rel: str, body: str) -> None:
    """Hygiene: the ways a page can be technically loadable and still useless."""
    if REPLACEMENT in body:
        report.add(
            ERROR,
            "hygiene/replacement-char",
            rel,
            "contains U+FFFD: a binary file was read as text and became a concept",
        )

    prose = prose_only(body)
    # Tags are only a finding in *prose*: a page that documents HTML must be allowed to
    # show it inside a code fence, so the code blocks are the parts blanked here. The
    # first version blanked the prose instead and searched the code, which reported the
    # documentation and missed the defect.
    if TAG.search(prose):
        report.add(WARN, "hygiene/inline-html", rel, "raw HTML in the body: markup is indexed as prose, so `div`/`class` become search terms")
    if ENTITY.search(prose):
        report.add(WARN, "hygiene/entity", rel, "un-decoded HTML entities in the body (`&mdash;` and friends)")
    if ANCHOR_ARTIFACT.search(prose):
        report.add(WARN, "hygiene/anchor-artifact", rel, "generator anchor artifacts (`[\\#](#...)`) left in prose")
    if any(marker in body for marker in MOJIBAKE):
        report.add(WARN, "hygiene/mojibake", rel, "mojibake (UTF-8 decoded twice, e.g. `Â`)")
    if len(body.strip()) < MIN_BODY_CHARS:
        report.add(WARN, "hygiene/short-body", rel, f"body is {len(body.strip())} characters: a stub that still costs a concept id")
    if len(body) > LARGE_PAGE_CHARS:
        report.add(INFO, "hygiene/large-page", rel, f"{len(body)} characters: `read_doc` truncates at {LARGE_PAGE_CHARS} by default")


def prose_only(body: str) -> str:
    """
    The body with fenced code blanked out.

    The exact inverse of `fetch.outside_code_fences`, which transforms the prose and
    keeps the code. Inline markup inside a fence is documentation *about* markup, so
    checking it would report every page that teaches HTML.
    """
    parts = CODE_FENCE.split(body)
    for index in range(1, len(parts), 2):  # odd indices are the fenced blocks
        parts[index] = ""
    return "".join(parts)


def check_index(report: LintReport, rel: str, body: str) -> None:
    """§8 — entries SHOULD include the description from the linked concept."""
    entries = 0
    described = 0
    for line in body.splitlines():
        match = INDEX_ENTRY.match(line)
        if not match:
            continue
        entries += 1
        if match.group("rest").strip().startswith("-"):
            described += 1
    if entries and described == 0:
        report.add(
            WARN,
            "index/no-descriptions",
            rel,
            f"{entries} entries and none carries a description; §8 asks index entries to "
            "repeat the linked concept's frontmatter description",
        )
    elif entries and described < entries // 2:
        report.add(WARN, "index/few-descriptions", rel, f"only {described} of {entries} entries carry a description (§8)")


def lint_corpus(root: Path, max_findings: int = 0) -> LintReport:
    report = LintReport(root=str(root))
    bundle_dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []

    for bundle in bundle_dirs:
        report.bundles += 1
        has_index = (bundle / RESERVED_INDEX).is_file()
        if not has_index:
            report.add(WARN, "structure/no-index", bundle.name, "no index.md: nothing can be discovered by progressive disclosure at the bundle root")

        for path in sorted(bundle.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                report.add(ERROR, "structure/unreadable", rel, f"cannot read: {error}")
                continue

            front, body = strip_frontmatter(text)
            name = path.name
            is_index = name == RESERVED_INDEX
            if is_index:
                report.indexes += 1
                check_index(report, rel, body)
            elif name != RESERVED_LOG:
                report.concepts += 1

            # A file that opens with `---` but yields no mapping had unparseable
            # frontmatter: `strip_frontmatter` returns {} and keeps the body, which is
            # right for building and wrong to pass over silently here.
            if not front and text.lstrip().startswith("---"):
                report.add(ERROR, "okf/frontmatter-unparseable", rel, "frontmatter block did not parse as a YAML mapping")

            check_frontmatter(report, rel, front, body, is_index, is_root_index=path.parent == bundle)
            check_body(report, rel, body)

            if max_findings and len(report.findings) >= max_findings:
                report.add(INFO, "limit/reached", rel, f"stopped after {max_findings} findings")
                return report

    return report


def render(report: LintReport) -> str:
    lines: list[str] = []
    lines.append(f"OKF lint: {report.root}")
    lines.append(
        f"  {report.bundles} bundles · {report.concepts} concepts · {report.indexes} index files"
    )
    counts: dict[str, int] = {}
    for finding in report.findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    if not report.findings:
        lines.append("  no findings: the corpus follows the spec")
        return "\n".join(lines)

    lines.append(
        f"  {len(report.errors)} errors · {len(report.warnings)} warnings · {len(report.infos)} notes"
    )
    lines.append("")
    for severity, group in (("ERROR", report.errors), ("WARN", report.warnings), ("NOTE", report.infos)):
        if not group:
            continue
        lines.append(f"{severity} ({len(group)})")
        shown = group[:40]
        for finding in shown:
            lines.append(f"  {finding.code:34} {finding.path}")
            lines.append(f"  {'':34} {finding.message}")
        if len(group) > len(shown):
            lines.append(f"  {'':34} … and {len(group) - len(shown)} more")
        lines.append("")
    lines.append("by code")
    for code, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:6}  {code}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check an OKF corpus against the published specification.")
    parser.add_argument("root", help="the corpus root (a directory of bundles)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--max-findings", type=int, default=0, help="stop after this many findings")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    report = lint_corpus(root, max_findings=args.max_findings)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
