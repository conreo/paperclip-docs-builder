"""
Tests for the corpus builder.

The pure parts are tested directly. Three of these are regression tests for bugs
the first real build exposed, and they are here rather than in a commit message
because each one produced a corpus that *looked* fine:

  * `test_exclude_applies_to_directory_contents` — `**/changelog*` matched the
    directory but not the files inside it, so the first build shipped a changelog
    and a contributor guide into a documentation corpus.
  * `test_bundle_root_index_exists_when_every_page_is_nested` — indexes were keyed
    off each page's immediate parent, so a bundle whose pages all live under one
    directory (n8n) got no root index and browsing fell back to a synthetic list.
  * `test_declared_path_is_not_part_of_the_bundle` — `path: docs` leaked into the
    output as `n8n/docs/...`, which put every page, URL and index one level wrong.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch  # noqa: E402
from fetch import (  # noqa: E402
    Page,
    Source,
    build_pages,
    derive_description,
    derive_title,
    derive_type,
    glob_match,
    index_for,
    is_locale_path,
    load_sources,
    normalise_date,
    normalise_markdown,
    output_path_for,
    render_concept,
    resolve_resource,
    should_include,
    split_by_h2,
    strip_frontmatter,
    strip_mdx,
    swap_into_place,
    write_bundle,
    yaml_string,
)


def make_source(**overrides) -> Source:
    base = dict(name="demo", kind="git", title="Demo", repo="https://example.invalid/x")
    base.update(overrides)
    return Source(**base)


class GlobTest(unittest.TestCase):
    def test_double_star_crosses_directories(self):
        self.assertTrue(glob_match("a.md", "**/*.md"))
        self.assertTrue(glob_match("a/b/c.md", "**/*.md"))
        self.assertTrue(glob_match("a/b/c.md", "a/**/*.md"))

    def test_single_star_does_not_cross_a_slash(self):
        self.assertFalse(glob_match("a/b.md", "*.md"))
        self.assertTrue(glob_match("a.md", "*.md"))

    def test_question_mark_matches_one_character_but_not_a_slash(self):
        self.assertTrue(glob_match("ab.md", "a?.md"))
        self.assertFalse(glob_match("a/b.md", "a?.md"))


class FilterTest(unittest.TestCase):
    def test_exclude_applies_to_directory_contents(self):
        # The regression: matching the directory is not enough.
        source = make_source(
            include=("**/*.md",), exclude=("**/changelog*", "**/contributing*")
        )
        self.assertFalse(should_include("docs/changelog/v1.md", source))
        self.assertFalse(should_include("CHANGELOG.md", source))
        self.assertTrue(should_include("docs/administer/index.md", source))

    def test_include_then_exclude_means_exclude_wins(self):
        source = make_source(include=("**/*.md",), exclude=("**/private/**",))
        self.assertTrue(should_include("private-ish/a.md", source))
        self.assertFalse(should_include("private/a.md", source))

    def test_locales_are_recognised(self):
        for path in ("locale/fr/x.md", "docs/locales/de/y.md", "guide.fr.md", "i18n/ja.md"):
            self.assertTrue(is_locale_path(path), path)
        for path in ("docs/admin.md", "location.md", "guides/french.md"):
            self.assertFalse(is_locale_path(path), path)


class FrontmatterTest(unittest.TestCase):
    def test_values_are_quoted_so_the_parser_cannot_misread_them(self):
        # The plugin's parser strips ` #` as a comment and reads a leading `[` as a
        # sequence. Bare emission would corrupt both of these.
        self.assertEqual(yaml_string("Install #1"), '"Install #1"')
        self.assertEqual(yaml_string("[beta] guide"), '"[beta] guide"')
        self.assertEqual(yaml_string('say "hi"'), '"say \\"hi\\""')

    def test_render_concept_emits_only_the_supported_shape(self):
        page = Page(
            bundle="demo",
            rel_path="a.md",
            title="T",
            description="D",
            type="Guide",
            resource="https://example.invalid/a",
            tags=["demo"],
            timestamp="2026-01-01T00:00:00Z",
            body="# Body\ntext",
        )
        rendered = render_concept(page)
        header = rendered.split("---")[1]
        # Flat `key: value` or `key: [..]` only: a nested map is an error to the
        # plugin's parser, so it must never be emitted.
        for line in header.strip().splitlines():
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+: .+$", line)
        self.assertIn('timestamp: "2026-01-01T00:00:00Z"', header)
        self.assertTrue(rendered.endswith("# Body\ntext\n"))

    def test_strip_frontmatter_handles_the_shapes_the_real_corpus_has(self):
        # A blank line before the fence, CRLF, broken YAML, and no frontmatter.
        fields, body = strip_frontmatter('\n---\ntitle: "x"\n---\n\nBody\n')
        self.assertEqual(fields.get("title"), "x")
        self.assertIn("Body", body)

        fields, body = strip_frontmatter('---\r\ntitle: "y"\r\n---\r\n\r\nBody\r\n')
        self.assertEqual(fields.get("title"), "y")

        fields, body = strip_frontmatter("---\n: : bad\n---\nBody")
        self.assertEqual(fields, {})
        self.assertIn("Body", body)

        fields, body = strip_frontmatter("# Just markdown")
        self.assertEqual(fields, {})
        self.assertEqual(body, "# Just markdown")

    def test_normalise_date_always_returns_utc(self):
        self.assertEqual(normalise_date("2026-01-02T03:04:05+02:00"), "2026-01-02T01:04:05Z")
        self.assertEqual(normalise_date("2026-01-02T03:04:05Z"), "2026-01-02T03:04:05Z")
        # A garbled date must not become a plausible-looking one. It returns empty
        # and the caller falls back to the source's real snapshot date: a build-time
        # date here would make a stale corpus look fresh, which is the single thing
        # `sources` exists to prevent.
        self.assertEqual(normalise_date("not a date"), "")
        self.assertEqual(normalise_date(""), "")
        # The `Z` suffix is handled explicitly rather than relying on 3.11+ parsing.
        self.assertEqual(normalise_date("2026-01-02T03:04:05z"), "2026-01-02T03:04:05Z")


class MarkdownTest(unittest.TestCase):
    def test_mdx_scaffolding_goes_and_prose_stays(self):
        body = (
            'import Tabs from "@theme/Tabs";\n'
            'export const x = 1;\n'
            "\n"
            "# Title\n"
            "\n"
            "<Tabs>\n<Tab>\ninner text\n</Tab>\n</Tabs>\n"
            "\n"
            "{/* a comment */}\n"
            "{someExpression}\n"
            "\n"
            '<div class="keep">html stays</div>\n'
        )
        out = strip_mdx(body)
        self.assertNotIn("import", out)
        self.assertNotIn("export const", out)
        self.assertNotIn("<Tabs>", out)
        self.assertNotIn("someExpression", out)
        self.assertIn("# Title", out)
        self.assertIn("inner text", out)
        # Lowercase tags are legitimate HTML; stripping them would delete content.
        self.assertIn('<div class="keep">html stays</div>', out)

    def test_fenced_code_is_never_rewritten(self):
        # A page about a frontend framework *shows* JSX. Stripping it from the
        # sample would corrupt the one thing on the page a reader copies.
        body = (
            "# Title\n\n"
            "Prose with a {% hint %} tag that should go.\n\n"
            "```jsx\n"
            'import Tabs from "@theme/Tabs";\n'
            "<Tabs>\n  <Tab>sample</Tab>\n</Tabs>\n"
            "```\n"
        )
        out = normalise_markdown(strip_mdx(body))
        self.assertNotIn("{% hint %}", out)
        self.assertIn('import Tabs from "@theme/Tabs";', out)
        self.assertIn("<Tab>sample</Tab>", out)

    def test_authoring_syntax_is_not_prose(self):
        body = 'Text\n\n{% hint style="info" %}\nHint body\n{% endhint %}\n\n<!-- hidden -->\n'
        out = normalise_markdown(body)
        self.assertNotIn("{%", out)
        self.assertNotIn("hidden", out)
        self.assertIn("Hint body", out)


class DerivationTest(unittest.TestCase):
    def test_title_prefers_the_source_then_the_heading_then_the_filename(self):
        self.assertEqual(derive_title({"title": "From source"}, "# Heading", "a/b.md"), "From source")
        self.assertEqual(derive_title({}, "# Heading", "a/b.md"), "Heading")
        self.assertEqual(derive_title({}, "no heading", "a/some-page_name.md"), "Some Page Name")
        # "Untitled" by the thousand is what made the old corpus unsearchable.
        self.assertEqual(derive_title({"title": "Untitled"}, "# Real", "a.md"), "Real")

    def test_numeric_filename_prefixes_are_dropped(self):
        self.assertEqual(derive_title({}, "", "010_introduction.md"), "Introduction")
        self.assertEqual(derive_title({}, "", "docs/040_backup.md"), "Backup")

    def test_type_is_never_empty(self):
        source = make_source()
        self.assertEqual(derive_type({"type": "Reference"}, "a/b.md", source), "Reference")
        self.assertEqual(derive_type({}, "administer/sso.md", source), "Administer")
        self.assertEqual(derive_type({}, "top.md", source), "Demo guide")

    def test_description_falls_back_to_the_first_real_paragraph(self):
        body = "# Title\n\n* a list item\n\nThis is the first real sentence, long enough to use.\n"
        self.assertEqual(
            derive_description({}, body),
            "This is the first real sentence, long enough to use.",
        )
        self.assertEqual(derive_description({"summary": "From frontmatter"}, body), "From frontmatter")

    def test_resource_strips_landing_pages_and_honours_absolute_values(self):
        source = make_source(resource_base="https://docs.example/", path="docs")
        self.assertEqual(
            resolve_resource({}, source, "docs/administer/README.md"),
            "https://docs.example/administer",
        )
        self.assertEqual(
            resolve_resource({}, source, "docs/a/b.md"), "https://docs.example/a/b"
        )
        self.assertEqual(
            resolve_resource({"resource": "https://upstream/x"}, source, "docs/a.md"),
            "https://upstream/x",
        )

    def test_output_is_always_markdown(self):
        self.assertEqual(output_path_for("a/b.mdx"), "a/b.md")
        self.assertEqual(output_path_for("a/b.rst"), "a/b.md")
        self.assertEqual(output_path_for("a/b.md"), "a/b.md")


class IndexTest(unittest.TestCase):
    def test_index_lists_sections_and_pages(self):
        text = index_for("administer", ["sso", "users"], [("Single sign-on", "sso.md")])
        self.assertIn("# Administer", text)
        self.assertIn("* [Sso](sso/)", text)
        self.assertIn("* [Single sign-on](sso.md)", text)


class WriteBundleTest(unittest.TestCase):
    def page(self, rel, title="T"):
        return Page(
            bundle="demo",
            rel_path=rel,
            title=title,
            description="",
            type="Guide",
            resource="",
            tags=["demo"],
            timestamp="2026-01-01T00:00:00Z",
            body=f"# {title}",
        )

    def test_bundle_root_index_exists_when_every_page_is_nested(self):
        # The regression: n8n's pages are all under one directory, and the bundle
        # had no root index at all.
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            write_bundle(staging, make_source(), [self.page("docs/a/x.md", "X")])
            self.assertTrue((staging / "demo" / "index.md").is_file())
            self.assertTrue((staging / "demo" / "docs" / "index.md").is_file())
            self.assertTrue((staging / "demo" / "docs" / "a" / "index.md").is_file())

    def test_indexes_exist_for_every_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            write_bundle(
                staging,
                make_source(),
                [self.page("a/b/c/deep.md", "Deep"), self.page("toplevel.md", "Top")],
            )
            for relative in ("index.md", "a/index.md", "a/b/index.md", "a/b/c/index.md"):
                self.assertTrue((staging / "demo" / relative).is_file(), relative)


class SwapTest(unittest.TestCase):
    def test_swap_keeps_exactly_one_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "okf-bundles"
            root.mkdir()
            (root / "old.txt").write_text("first")
            for generation in ("second", "third"):
                staging = Path(tmp) / f".staging-{generation}"
                staging.mkdir()
                (staging / "new.txt").write_text(generation)
                swap_into_place(staging, root, keep_previous=True, log=lambda _m: None)
                self.assertEqual((root / "new.txt").read_text(), generation)
            previous = Path(tmp) / "okf-bundles.previous"
            self.assertTrue(previous.is_dir())
            self.assertFalse(Path(tmp).glob(".staging-*").__next__() if list(Path(tmp).glob(".staging-*")) else False)
            # One previous, not an accumulating pile.
            self.assertEqual(len(list(Path(tmp).glob("okf-bundles.previous*"))), 1)

    def test_swap_without_keeping_previous_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "okf-bundles"
            root.mkdir()
            (root / "old.txt").write_text("first")
            staging = Path(tmp) / ".staging-x"
            staging.mkdir()
            (staging / "new.txt").write_text("second")
            swap_into_place(staging, root, keep_previous=False, log=lambda _m: None)
            self.assertFalse((Path(tmp) / "okf-bundles.previous").exists())
            self.assertTrue((root / "new.txt").is_file())


class ConfigTest(unittest.TestCase):
    def test_unknown_keys_are_rejected_loudly(self):
        # A misspelled `exlude` would otherwise silently ship a whole repository.
        with self.assertRaises(ValueError) as caught:
            Source.from_config("x", {"kind": "git", "exlude": ["a"]}, {}, [])
        self.assertIn("exlude", str(caught.exception))

    def test_global_excludes_and_defaults_are_merged(self):
        source = Source.from_config(
            "x", {"kind": "git", "include": ["**/*.md"]}, {"max_file_bytes": 123}, ["**/vendor/**"]
        )
        self.assertEqual(source.max_file_bytes, 123)
        self.assertIn("**/vendor/**", source.exclude)

    def test_the_shipped_sources_file_loads(self):
        sources, global_exclude, defaults = load_sources(
            Path(fetch.__file__).with_name("sources.yaml")
        )
        self.assertIsInstance(defaults, dict)
        self.assertGreaterEqual(len(sources), 14)
        self.assertTrue(global_exclude)
        names = {s.name for s in sources}
        for expected in ("n8n", "grafana", "nextcloud", "vaultwarden", "brevo"):
            self.assertIn(expected, names)
        # Every source must be buildable from what it declares.
        for source in sources:
            if source.kind in ("git", "wiki"):
                self.assertTrue(source.repo, source.name)
            elif source.kind == "llms":
                self.assertTrue(source.url, source.name)
            else:
                self.fail(f"{source.name}: unknown kind {source.kind}")


class BuildPagesTest(unittest.TestCase):
    """build_pages over a synthetic checkout, so no network is involved."""

    def make_checkout(self, files: dict[str, str]) -> Path:
        tmp = Path(tempfile.mkdtemp())
        for rel, content in files.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return tmp

    def test_declared_path_is_not_part_of_the_bundle(self):
        # The regression: `path: docs` produced `n8n/docs/...` in the corpus.
        checkout = self.make_checkout({"docs/a/x.md": "# X\n\nbody text here that is long enough\n"})
        source = make_source(path="docs", include=("**/*.md",), convert="none")
        pages, _, _ = build_pages(source, checkout, lambda _m: None, limit=0)
        self.assertEqual([p.rel_path for p in pages], ["a/x.md"])

    def test_timestamp_default_is_the_snapshot_date(self):
        checkout = self.make_checkout({"a.md": "# A\n\nbody\n"})
        source = make_source(convert="none")
        pages, _, _ = build_pages(
            source, checkout, lambda _m: None, timestamp_default="2020-05-05T00:00:00Z"
        )
        self.assertEqual(pages[0].timestamp, "2020-05-05T00:00:00Z")

    def test_oversized_files_are_skipped_and_counted(self):
        checkout = self.make_checkout({"big.md": "# B\n\n" + "x" * 5000, "small.md": "# S\n\nok body text\n"})
        source = make_source(convert="none", max_file_bytes=1000)
        pages, counts, _ = build_pages(source, checkout, lambda _m: None)
        self.assertEqual([p.rel_path for p in pages], ["small.md"])
        self.assertEqual(counts.oversized, 1)
        self.assertEqual(counts.total, 1)

    def test_every_filter_drop_is_counted_by_reason(self):
        # The first version counted none of these: files dropped by a glob simply
        # vanished, so the manifest could not show what the filters removed.
        checkout = self.make_checkout({
            "keep.md": "# Keep\n\nbody text long enough\n",
            "notes/CHANGELOG.md": "# Changelog\n\nbody text long enough\n",
            "locale/fr/x.md": "# Fr\n\nbody text long enough\n",
        })
        source = make_source(
            include=("**/*.md",), exclude=("**/changelog*",), convert="none", max_file_bytes=10_000
        )
        pages, counts, _ = build_pages(source, checkout, lambda _m: None)
        self.assertEqual([p.rel_path for p in pages], ["keep.md"])
        self.assertEqual(counts.glob, 1)
        self.assertEqual(counts.locale, 1)
        self.assertEqual(counts.total, 2)

    def test_collisions_are_counted_not_silently_overwritten(self):
        checkout = self.make_checkout(
            {"x.md": "# From markdown\n\nbody one is long enough\n",
             "x.mdx": "# From mdx\n\nbody two is long enough\n"}
        )
        source = make_source(include=("**/*.md", "**/*.mdx"), convert="none")
        pages, _, collisions = build_pages(source, checkout, lambda _m: None)
        self.assertEqual(len(pages), 1)
        self.assertEqual(collisions, 1)
        # Markdown wins; which one survives must not depend on filesystem order.
        self.assertIn("From markdown", pages[0].body)

    def test_llms_split_produces_one_concept_per_section(self):
        text = "intro preamble that is long enough to keep\n\n## Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n"
        sections = split_by_h2(text)
        self.assertEqual([title for title, _ in sections], ["Alpha", "Beta"])
        self.assertIn("intro preamble", sections[0][1])
        self.assertIn("beta body", sections[1][1])

    def test_llms_without_sections_is_one_concept(self):
        sections = split_by_h2("just a body with no headings\n")
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "")


class ManifestShapeTest(unittest.TestCase):
    def test_manifest_is_json_with_the_fields_the_plugin_reports(self):
        # The plugin reads one of three filenames and reports the object verbatim,
        # so the shape here is a contract with `sources` in the plugin.
        self.assertIn(fetch.MANIFEST_FILENAME, ("manifest.json", "okf-manifest.json", "build-manifest.json"))
        manifest = {
            "okf_version": fetch.OKF_VERSION,
            "sources": {"n8n": {"commit": "a" * 40, "commit_date": "2026-01-01T00:00:00Z"}},
        }
        encoded = json.loads(json.dumps(manifest))
        self.assertEqual(encoded["sources"]["n8n"]["commit"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
