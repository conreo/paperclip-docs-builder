#!/usr/bin/env python3
"""
Build an OKF documentation corpus from declared sources.

One pipeline, declarative adapters. Every source goes through the same stages —
fetch, filter, convert, frontmatter, write, manifest — and the only thing that
varies per source is the entry in `sources.yaml`. That is deliberate: the previous
pipeline in this project turned each source into code, and the result was a 20 KB
`fix_docs.py` plus early stages that can no longer be re-run at all. A source that
needs special handling should need a *field*, not a function.

The output is what the `paperclip-docs` plugin serves:

    <out>/                      the corpus root the plugin is pointed at
      manifest.json             which revision each bundle came from, and when
      n8n/                      one bundle per source
        index.md                navigation for the directory
        010_introduction.md     a concept: markdown + OKF frontmatter

Two properties matter more than speed:

  * **Reproducible.** Each source records the resolved commit and its date, so a
    citation can be traced and a rebuild can be compared. Unpinned sources follow
    the default branch and are marked as such in the manifest.
  * **Degrading, not failing.** One broken source must not cost you the other
    thirteen. A source that fails keeps its previous bundle, which is marked
    `stale` in the manifest — and `sources` in the plugin then tells the agent the
    bundle is old rather than silently answering from it.

Usage
    ./fetch.py --out ./out/okf-bundles                # every source
    ./fetch.py --bundle n8n --bundle erpnext          # just these
    ./fetch.py --check                                # resolve sources, write nothing
    ./fetch.py --limit-pages 20                       # smoke run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

BUILDER_VERSION = "0.2.0"

#: The source kinds this builder knows how to acquire. Kept here so the runner can
#: refuse a request naming a kind it cannot honour, rather than silently fetching
#: nothing and reporting a smaller corpus.
SOURCE_KINDS = ("git", "wiki", "llms", "local")
OKF_VERSION = "0.1"
MANIFEST_FILENAME = "manifest.json"

#: The optional vector index. Two files, deliberately boring: one metadata document
#: and one flat float32 matrix. The plugin reads them with `node:fs` and does the
#: arithmetic itself, so this needs no database, no native module and no server —
#: which is the whole reason a corpus with a few thousand pages can be searched
#: semantically by a worker that is not allowed to spawn anything.
EMBEDDINGS_JSON = "embeddings.json"
EMBEDDINGS_BIN = "embeddings.bin"
EMBEDDINGS_SCHEMA = 1
#: Characters of a concept embedded. Matches how much of the body is ranked, so the
#: vector and the keyword index see the same document.
EMBED_TEXT_CHARS = 2_000
# A source whose upstream has not moved in a year is usually a source that has
# relocated, not one that is finished. It is reported, not treated as an error.
UPSTREAM_STALE_DAYS = 365
INDEX_FILENAME = "index.md"

# The frontmatter the plugin's parser understands is a narrow subset: flat scalars
# and sequences, no nested maps. Anything else is reported as a parse error, so the
# renderer below never emits it.
LOCALE_PATTERN = re.compile(
    r"(^|/)(locale|locales|i18n|l10n|translations?|lang)(/|$)"
    r"|\.(fr|de|es|it|pt|pt-br|nl|ru|pl|ja|ko|zh|zh-cn|zh-tw|tr|cs|sv|da|fi|nb|hu|uk|ar|he|id|vi|th)\.(md|mdx|rst)$",
    re.IGNORECASE,
)

MDX_IMPORT = re.compile(r"^\s*(import|export)\s.+?;?\s*$", re.MULTILINE)
MDX_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)
# Component *tags* are dropped, never their contents. A `<Tabs>`/`<Tab>` pair
# usually wraps the prose an agent needs; deleting the block because it looks like
# JSX deleted real documentation in the first version of this function. Lowercase
# tags are left alone entirely — those are legitimate HTML.
MDX_TAG = re.compile(r"</?[A-Z][A-Za-z0-9_.]*(?:\s[^<>]*?)?/?>", re.MULTILINE)
MDX_EXPRESSION = re.compile(r"^[ \t]*\{[^\n]*\}[ \t]*$", re.MULTILINE)
# GitBook ships its own syntax, on its own line or inline mid-sentence. `{% hint %}`
# / `{% endhint %}` are the common case; the prose between them is worth keeping, so
# the tags go and the text stays.
GITBOOK_TAG = re.compile(r"\{%[^%]*%\}")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RST_TOCTREE = re.compile(r"^[ \t]*\.\.\s+(toctree|include|literalinclude)::.*$", re.MULTILINE)
CODE_FENCE = re.compile(r"(^[ \t]*```.*?^[ \t]*```[ \t]*$)", re.DOTALL | re.MULTILINE)
FRONTMATTER_BLOCK = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


# --------------------------------------------------------------------------- model


@dataclasses.dataclass
class Source:
    """One entry from sources.yaml, with defaults applied."""

    name: str
    kind: str
    title: str = ""
    repo: str = ""
    url: str = ""
    fallback_url: str = ""
    ref: str = ""
    path: str = ""
    include: tuple[str, ...] = ("**/*.md",)
    exclude: tuple[str, ...] = ()
    convert: str = "auto"
    tags: tuple[str, ...] = ()
    resource_base: str = ""
    max_file_bytes: int = 262144
    max_pages: int = 0
    drop_locales: bool = True
    split: str = "single"
    #: `kind: local` — a folder on this host, for a project's own documentation.
    #: The plugin's registry calls it `local`; this is the same idea the reference
    #: provisioning system expressed as `LOCAL_DOCS`.
    folder: str = ""
    # Declared facts about the upstream, carried into the manifest. A source that
    # has stopped moving is the ordinary way a corpus rots — not a build failure —
    # so it is recorded rather than discovered later by an agent reading 2021 docs
    # as though they were current.
    archived: bool = False
    note: str = ""

    @classmethod
    def from_config(cls, name, raw, defaults, global_exclude):
        merged = {**defaults, **(raw or {})}
        known = {f.name for f in dataclasses.fields(cls)} - {"name"}
        unknown = set(merged) - known
        if unknown:
            # A typo in sources.yaml should be loud: a silently ignored `exlude`
            # would quietly ship the whole repository.
            raise ValueError(f"source '{name}' has unknown keys: {', '.join(sorted(unknown))}")
        include = tuple(merged.get("include") or defaults.get("include") or ("**/*.md",))
        exclude = tuple(global_exclude) + tuple(merged.get("exclude") or ())
        return cls(
            name=name,
            kind=merged.get("kind", "git"),
            title=merged.get("title") or name,
            repo=merged.get("repo", ""),
            url=merged.get("url", ""),
            fallback_url=merged.get("fallback_url", ""),
            ref=merged.get("ref", ""),
            path=(merged.get("path") or "").strip("/"),
            # `folder` is a filesystem path, not a repository path: it is not
            # stripped of leading separators, because an absolute one is legal.

            include=include,
            exclude=exclude,
            convert=merged.get("convert", "auto"),
            tags=tuple(merged.get("tags") or ()),
            resource_base=merged.get("resource_base", ""),
            max_file_bytes=int(merged.get("max_file_bytes", 262144)),
            max_pages=int(merged.get("max_pages", 0) or 0),
            drop_locales=bool(merged.get("drop_locales", True)),
            split=merged.get("split", "single"),
            folder=str(merged.get("folder", "") or ""),
            archived=bool(merged.get("archived", False)),
            note=str(merged.get("note", "")),
        )


@dataclasses.dataclass
class Page:
    """A concept ready to be rendered."""

    bundle: str
    rel_path: str  # relative to the bundle, always .md
    title: str
    description: str
    type: str
    resource: str
    tags: list[str]
    timestamp: str
    body: str
    # Rendered once, when the source is built, and reused when the bundle is
    # written. Rendering twice for a byte count was pure waste on 4,000 pages.
    rendered: str = ""


@dataclasses.dataclass
class FilterCounts:
    """
    What the filters removed, by reason.

    Reported in the manifest because the filters are the point of owning this
    pipeline: a corpus that cannot say what it excluded cannot be tuned, and the
    first version counted none of these — files dropped by a glob simply vanished.
    """

    glob: int = 0
    locale: int = 0
    oversized: int = 0
    unreadable: int = 0
    empty: int = 0

    @property
    def total(self) -> int:
        return self.glob + self.locale + self.oversized + self.unreadable + self.empty

    def as_dict(self) -> dict:
        return {
            "by_glob": self.glob,
            "by_locale": self.locale,
            "oversized": self.oversized,
            "unreadable": self.unreadable,
            "empty": self.empty,
            "total": self.total,
        }


@dataclasses.dataclass
class SourceResult:
    name: str
    ok: bool
    error: str = ""
    commit: str = ""
    ref: str = ""
    pinned: bool = False
    commit_date: str = ""
    pages: int = 0
    bytes: int = 0
    counts: "FilterCounts" = dataclasses.field(default_factory=FilterCounts)
    collisions: int = 0
    seconds: float = 0.0
    warnings: list[str] = dataclasses.field(default_factory=list)
    upstream_age_days: int | None = None
    # Carried from the worker thread to the writer; a build holds every page of a
    # source at once, which for the largest source here is a few megabytes.
    pages_data: list["Page"] = dataclasses.field(default_factory=list)


# ------------------------------------------------------------------- pure helpers


def glob_match(rel_path: str, pattern: str) -> bool:
    """
    Match a relative POSIX path against a glob where `**` crosses directories.

    fnmatch alone cannot express this: `fnmatch('a/b/c.md', '**/*.md')` is false
    because `*` does not cross `/`, and every include rule in sources.yaml is
    written the way people expect it to work.

    Matching is case-insensitive because the excludes are written the way people
    write them (`**/changelog*`) while the files are not (`CHANGELOG.md`), and
    missing one means shipping a changelog into a documentation corpus.
    """
    rel_path = rel_path.lower()
    pattern = pattern.lower()
    if pattern.startswith("**/"):
        if glob_match(rel_path, pattern[3:]):
            return True
    regex = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                # `**/` also matches zero directories, so `**/x.md` matches `x.md`.
                if pattern.startswith("**/", index):
                    regex += "(?:.*/)?"
                    index += 3
                    continue
                regex += ".*"
                index += 2
                continue
            regex += "[^/]*"
            index += 1
            continue
        if char == "?":
            regex += "[^/]"
            index += 1
            continue
        regex += re.escape(char)
        index += 1
    return re.fullmatch(regex, rel_path) is not None


def should_include(rel_path: str, source: Source) -> bool:
    """
    Include rules first, then excludes: an exclude always wins.

    An exclude also applies to everything *under* the match. `**/changelog*` has to
    remove `docs/changelog/v1.md`, and the naive glob does not: `*` stops at a
    slash, so the pattern matches the directory but not its contents. That is how
    the first build shipped a changelog and a contributor guide into the corpus.
    """
    if not any(glob_match(rel_path, pattern) for pattern in source.include):
        return False
    for pattern in source.exclude:
        if glob_match(rel_path, pattern) or glob_match(rel_path, pattern + "/**"):
            return False
    return True


def is_locale_path(rel_path: str) -> bool:
    return bool(LOCALE_PATTERN.search(rel_path))


def yaml_string(value: str) -> str:
    """
    Render a string as a double-quoted YAML scalar.

    Always quoted, never plain. The plugin's parser strips ` #` as a comment and
    reads a leading `[` as a sequence, so a title like `Install #1 [beta]` would
    corrupt the frontmatter if it were emitted bare.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(yaml_string(v) for v in values) + "]"


def render_concept(page: Page) -> str:
    """The on-disk form of one concept: OKF frontmatter, then the body."""
    lines = [
        "---",
        f"type: {yaml_string(page.type)}",
        f"title: {yaml_string(page.title)}",
        f"description: {yaml_string(page.description)}",
        f"resource: {yaml_string(page.resource)}",
        f"tags: {yaml_list(page.tags)}",
        f"timestamp: {yaml_string(page.timestamp)}",
        f"okf_version: {yaml_string(OKF_VERSION)}",
        "---",
        "",
        page.body.rstrip() + "\n",
    ]
    return "\n".join(lines)


def strip_frontmatter(text: str) -> tuple[dict, str]:
    """Split a source file into its own frontmatter and its body. Never raises."""
    match = FRONTMATTER_BLOCK.match(text.replace("\r\n", "\n").replace("\r", "\n"))
    if not match:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        # A source file with broken frontmatter still has a body worth keeping.
        parsed = None
    body = text[match.end():]
    return (parsed if isinstance(parsed, dict) else {}), body


def outside_code_fences(body: str, transform):
    """
    Apply a transform only to the parts of a document that are not code.

    Documentation about a frontend framework *shows* JSX. Stripping a component tag
    out of a code sample would corrupt the one thing on the page a reader copies.
    """
    parts = CODE_FENCE.split(body)
    for index, part in enumerate(parts):
        if index % 2 == 0:  # odd indices are the fenced blocks themselves
            parts[index] = transform(part)
    return "".join(parts)


def strip_mdx(body: str) -> str:
    """Remove MDX scaffolding while keeping every word of prose."""

    def transform(text: str) -> str:
        text = MDX_COMMENT.sub("", text)
        text = MDX_IMPORT.sub("", text)
        text = MDX_TAG.sub("", text)
        text = MDX_EXPRESSION.sub("", text)
        return text

    return re.sub(r"\n{3,}", "\n\n", outside_code_fences(body, transform))


def normalise_markdown(body: str) -> str:
    """
    Remove authoring syntax that is not prose.

    Applied to every source after conversion. Search scores the body, so a corpus
    full of `{% hint style="info" %}` and HTML comments spends index space and
    snippet budget on markup no one reads.
    """
    def transform(text: str) -> str:
        text = GITBOOK_TAG.sub("", HTML_COMMENT.sub("", text))
        # A tag removed from the middle of a line leaves indentation behind.
        return re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)

    return re.sub(r"\n{3,}", "\n\n", outside_code_fences(body, transform))


def first_heading(body: str) -> str:
    match = re.search(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", body, re.MULTILINE)
    return match.group(1).strip() if match else ""


def first_prose(body: str, limit: int = 200) -> str:
    """A usable description when the source has none: the first real paragraph."""
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if not text or text.startswith("#") or text.startswith("<!--"):
            continue
        if text.startswith(("|", "```", ":::", ".. ", ">", "-", "*", "1.")):
            continue
        # A badge block is not a description. "![Build Status](…)" reduces to
        # punctuation and a URL, and a search result that reads `!Build Status !`
        # is worse than no description at all.
        if "![" in text:
            continue
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # links to their text
        text = re.sub(r"[`*_]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 24:
            return text[: limit - 1] + "…" if len(text) > limit else text
    return ""


def derive_title(frontmatter: dict, body: str, rel_path: str) -> str:
    """
    A title, in order of trust: the source's own, its first heading, the filename.

    `Untitled` is treated as absent because the old corpus emitted it by the
    thousand and search results titled "Untitled" are indistinguishable.
    """
    for key in ("title", "name", "sidebar_label", "label"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip() and not re.fullmatch(r"untitled", value.strip(), re.I):
            return value.strip()
    heading = first_heading(body)
    if heading:
        return heading
    stem = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    pretty = re.sub(r"[-_]+", " ", re.sub(r"^\d+[_\-.]*", "", stem)).strip()
    return pretty.title() if pretty else stem


def derive_type(frontmatter: dict, rel_path: str, source: Source) -> str:
    """The concept's kind. The plugin indexes pages by it, so it is never empty."""
    for key in ("type", "kind", "category", "doc_type"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:48]
    parts = [p for p in rel_path.split("/") if p]
    if len(parts) > 1:
        label = parts[0].replace("-", " ").replace("_", " ").strip()
        if label and not re.fullmatch(r"\d+", label):
            return label[:48].capitalize()
    return f"{source.title} guide"[:48] if source.title else "Guide"


def derive_description(frontmatter: dict, body: str) -> str:
    for key in ("description", "summary", "excerpt", "abstract"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value.strip())[:400]
    return first_prose(body)


def resolve_resource(frontmatter: dict, source: Source, rel_path: str) -> str:
    """A citable URL: the source's own if absolute, else base + path."""
    for key in ("resource", "canonical", "source", "url"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    if not source.resource_base:
        return source.repo or source.url
    rel = rel_path
    if source.path and rel.startswith(source.path + "/"):
        rel = rel[len(source.path) + 1 :]
    rel = re.sub(r"\.(md|mdx|rst)$", "", rel)
    # A directory's landing page is the directory itself. `.../administer/README`
    # is not a URL anyone can visit, and the plugin cites this field.
    rel = re.sub(r"/(index|readme)$", "/", rel, flags=re.IGNORECASE)
    rel = re.sub(r"^(index|readme)$", "", rel, flags=re.IGNORECASE)
    # Wiki page names contain spaces and parentheses. A citation with a raw space in
    # it is not a URL, and the plugin prints this field verbatim.
    return source.resource_base.rstrip("/") + "/" + urllib.parse.quote(rel.strip("/"), safe="/")


def output_path_for(rel_path: str) -> str:
    """Everything becomes `.md`; the plugin only reads markdown."""
    return re.sub(r"\.(mdx|rst)$", ".md", rel_path)


def navigation_body(subdirectories: list[str], pages: list[tuple[str, str]]) -> str:
    """The `Sections` and `Pages` lists, with no heading of their own."""
    lines: list[str] = []
    if subdirectories:
        lines += ["## Sections", ""]
        lines += [f"* [{child.replace('-', ' ').title()}]({child}/)" for child in subdirectories]
        lines.append("")
    if pages:
        lines += ["## Pages", ""]
        lines += [f"* [{label}]({target})" for label, target in pages]
        lines.append("")
    return "\n".join(lines).strip()


def index_for(directory: str, subdirectories: list[str], pages: list[tuple[str, str]]) -> str:
    """
    A navigation page for one directory, for directories the source has no page for.

    The plugin treats `index.md` as navigation: `list_docs` returns it for that
    level. One per directory is what makes browsing progressive instead of a flat
    dump of a thousand pages.
    """
    label = directory.rstrip("/").rsplit("/", 1)[-1]
    title = label.replace("-", " ").replace("_", " ").title() if label else "Documentation"
    body = navigation_body(subdirectories, pages)
    return f"# {title}\n\n{body}\n" if body else f"# {title}\n"


# ---------------------------------------------------------------------- adapters


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> str:
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(f"{cmd[0]} failed: {detail[-1] if detail else result.returncode}")
    return result.stdout


def normalise_date(raw: str) -> str:
    """
    A UTC `Z` timestamp, which is what the corpus's own dates look like.

    Returns "" when the input is not a date, rather than substituting the build
    time. `sources` reports corpus age from these, so a date fabricated at build
    time would make a stale snapshot look fresh — the one failure this field
    exists to prevent. Callers fall back to the source's real snapshot date.
    """
    try:
        # `Z` and `z` are both valid RFC 3339, and neither is understood by
        # `fromisoformat` before Python 3.11 — normalise it rather than depend on
        # the interpreter's version.
        parsed = dt.datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", raw.strip()))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


def age_in_days(timestamp: str) -> int | None:
    """How long ago a source last moved, from a UTC `Z` timestamp."""
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - parsed).days


def git_clone(source: Source, workdir: Path, log) -> tuple[Path, str, str, bool]:
    """
    Check out the source and return (checkout, commit, commit_date, pinned).

    Sparse and blob-filtered when a subdirectory is declared: `grafana/grafana` is
    hundreds of megabytes and only `docs/sources` is wanted. The cache is reused
    across runs, which is the difference between a rebuild taking minutes and
    taking an hour.
    """
    target = workdir / source.name
    pinned = bool(source.ref)
    ref = source.ref

    # The cache is keyed by source name, which is not the same thing as the
    # repository. Reusing a checkout after `repo` changed silently built from the
    # old upstream and recorded its commit as provenance — a relocation, which is
    # the drift this builder exists to catch, would have been invisible.
    cached_remote = ""
    if (target / ".git").is_dir():
        try:
            cached_remote = run(["git", "config", "--get", "remote.origin.url"], cwd=target).strip()
        except RuntimeError:
            cached_remote = ""
    if (target / ".git").is_dir() and cached_remote != source.repo:
        log(f"    checkout was {cached_remote or 'unknown'}, re-cloning for {source.repo}")
        shutil.rmtree(target, ignore_errors=True)

    if not (target / ".git").is_dir():
        shutil.rmtree(target, ignore_errors=True)
        cmd = ["git", "clone", "--quiet", "--depth", "1", "--single-branch"]
        if ref:
            cmd += ["--branch", ref]
        if source.path:
            cmd += ["--filter=blob:none", "--sparse"]
        cmd += [source.repo, str(target)]
        run(cmd)
        if source.path:
            run(["git", "sparse-checkout", "set", source.path], cwd=target)
        log(f"    cloned {source.repo}")
    else:
        # A depth-1 cache is refreshed with one shallow fetch and a hard reset.
        # A pinned tag never moves, so a failed fetch falls back to the cache
        # rather than failing a rebuild that would produce identical output.
        branch = ref or run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target).strip()
        if branch in ("", "HEAD"):
            branch = "HEAD"
        try:
            run(["git", "fetch", "--quiet", "--depth", "1", "origin", branch], cwd=target)
            run(["git", "reset", "--quiet", "--hard", "FETCH_HEAD"], cwd=target)
            if source.path:
                # A changed `path` needs materialising; the cached sparse config
                # still describes the old one.
                run(["git", "sparse-checkout", "set", source.path], cwd=target)
        except RuntimeError:
            if pinned:
                raise
            log(f"    using cached checkout (shallow fetch of {branch} failed)")

    commit = run(["git", "rev-parse", "HEAD"], cwd=target).strip()
    date = run(["git", "log", "-1", "--format=%cI"], cwd=target).strip()
    if not pinned:
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target).strip()
        ref = branch if branch and branch != "HEAD" else (ref or "default")
    return target, commit, normalise_date(date) or date.strip(), pinned


def iter_git_files(source: Source, checkout: Path):
    """Every candidate file, as (relative path, absolute path), in a stable order."""
    root = checkout / source.path if source.path else checkout
    if not root.is_dir():
        raise RuntimeError(f"declared path '{source.path}' does not exist in {source.repo}")
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Match the `.git` directory by path component. A substring test also
        # matches `.github`, which silently dropped those files.
        if ".git" in path.relative_to(checkout).parts:
            continue
        rel = path.relative_to(checkout).as_posix()
        found.append((rel, path))
    # Deterministic, and when `x.md` and `x.mdx` collide the markdown wins.
    priority = {".md": 0, ".mdx": 1, ".rst": 2}
    found.sort(key=lambda item: (item[0].rsplit(".", 1)[0], priority.get(Path(item[0]).suffix, 9), item[0]))
    return found


def fetch_llms(source: Source, log) -> tuple[str, str, str]:
    """Fetch a published llms.txt. Returns (text, url used, warning)."""
    for url in [source.url, source.fallback_url]:
        if not url:
            continue
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": f"paperclip-docs-builder/{BUILDER_VERSION}"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8", errors="replace")
            if not text.strip():
                raise RuntimeError("empty response")
            warning = "" if url == source.url else f"preferred url unavailable, used {url}"
            log(f"    fetched {url} ({len(text) // 1024} KB)")
            return text, url, warning
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, OSError) as error:
            log(f"    {url} -> {error}")
    return "", "", "no llms.txt available at the declared urls"


def split_by_h2(text: str) -> list[tuple[str, str]]:
    """Split one large document into (title, body) pairs on second-level headings."""
    lines = text.split("\n")
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if re.match(r"^##\s+\S", line):
            sections.append((line.lstrip("#").strip(), []))
            continue
        if sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    if not sections:
        return [("", text)]
    out = []
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if preamble and not out:
            body = "\n".join(preamble).strip() + "\n\n" + body
        out.append((title, body))
    return out


def convert_with_pandoc(text: str, source: Source) -> str:
    if shutil.which("pandoc") is None:
        raise RuntimeError(
            "pandoc is required for convert: rst but is not installed. "
            "Run the builder in its container image, or install pandoc."
        )
    result = subprocess.run(
        ["pandoc", "-f", "rst", "-t", "gfm", "--wrap=none"],
        input=RST_TOCTREE.sub("", text),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed: {result.stderr.strip().splitlines()[:1]}")
    return result.stdout


def build_pages(
    source: Source,
    checkout: Path | None,
    log,
    limit: int = 0,
    timestamp_default: str = "",
    warnings: list[str] | None = None,
) -> tuple[list[Page], FilterCounts, int]:
    """
    Turn a source's files into concepts.

    Returns (pages, counts, collisions). Skipping is normal and counted by reason:
    that is how filters and size caps show up in the manifest instead of silently
    disappearing.

    `timestamp_default` is the source's snapshot date — the commit date for a
    repository, the fetch time for a published file — so every concept in a bundle
    reports when the documentation was captured, not when the build ran.
    """
    timestamp_default = timestamp_default or dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    candidates: list[tuple[str, str, str]] = []  # rel, title_hint, body
    counts = FilterCounts()

    if source.kind == "llms":
        text, _, warning = fetch_llms(source, log)
        if not text:
            raise RuntimeError(warning or "llms source unavailable")
        if warning and warnings is not None:
            # e.g. LINE's llms-full.txt answers 403, so the bundle silently came
            # from llms.txt instead. That belongs in the manifest.
            warnings.append(warning)
        if source.split == "h2":
            for title, body in split_by_h2(text):
                label = title or source.title
                candidates.append((f"{slugify(label)}.md", label, body))
        else:
            candidates.append(("index-body.md", source.title, text))
    else:
        assert checkout is not None
        for rel, absolute in iter_git_files(source, checkout):
            if not should_include(rel, source):
                counts.glob += 1
                continue
            if source.drop_locales and is_locale_path(rel):
                counts.locale += 1
                continue
            try:
                size = absolute.stat().st_size
            except OSError:
                counts.unreadable += 1
                continue
            if size > source.max_file_bytes:
                counts.oversized += 1
                continue
            try:
                raw = absolute.read_text(encoding="utf-8", errors="replace")
            except OSError:
                counts.unreadable += 1
                continue
            candidates.append((rel, "", raw))

    pages: list[Page] = []
    collisions = 0
    seen: dict[str, str] = {}

    for rel, title_hint, raw in candidates:
        if not raw.strip():
            counts.empty += 1
            continue
        frontmatter, body = strip_frontmatter(raw)
        suffix = Path(rel).suffix.lower()
        if source.convert == "rst" or suffix == ".rst":
            try:
                body = convert_with_pandoc(body, source)
            except RuntimeError as error:
                raise RuntimeError(f"{rel}: {error}") from error
        elif source.convert in ("auto", "mdx") and suffix == ".mdx":
            body = strip_mdx(body)

        body = strip_frontmatter(body)[1] if body.lstrip().startswith("---") else body
        body = normalise_markdown(body)
        heading = first_heading(body)
        if not heading and not title_hint and len(body.strip()) < 40:
            counts.empty += 1
            continue

        # The declared `path` is a slice of the repository, not part of the bundle:
        # `path: docs` on n8n must not produce `n8n/docs/...`, or every URL and
        # every directory index is one level wrong.
        bundle_rel = rel
        if source.path and bundle_rel.startswith(source.path + "/"):
            bundle_rel = bundle_rel[len(source.path) + 1 :]

        out_rel = output_path_for(bundle_rel)
        if out_rel in seen:
            collisions += 1
            continue
        seen[out_rel] = rel

        pages.append(
            Page(
                bundle=source.name,
                rel_path=out_rel,
                title=derive_title(frontmatter, body, bundle_rel) if not title_hint else title_hint,
                description=derive_description(frontmatter, body),
                type=derive_type(frontmatter, bundle_rel, source),
                resource=resolve_resource(frontmatter, source, rel),
                tags=list(source.tags) or [source.name],
                timestamp=(
                    normalise_date(str(frontmatter["timestamp"]))
                    if frontmatter.get("timestamp")
                    else timestamp_default
                ),
                body=body,
            )
        )
        if limit and len(pages) >= limit:
            break

    if source.max_pages and len(pages) > source.max_pages:
        counts.oversized += len(pages) - source.max_pages
        pages = pages[: source.max_pages]
    return pages, counts, collisions


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "section"


# ------------------------------------------------------------------ orchestration


def write_bundle(staging: Path, source: Source, pages: list[Page]) -> int:
    """
    Write one bundle: its concepts, then navigation for every directory.

    A source's own landing page (`index.md` for a directory) *is* that directory's
    navigation. The first version wrote it and then overwrote it with a generated
    listing — 525 real pages across these sources were destroyed, and each index
    listed itself as one of its own children. Where the source has a landing page
    its content is kept, as a normal concept with its frontmatter, and the
    generated sections are appended underneath it.
    """
    bundle_dir = staging / source.name
    bundle_dir.mkdir(parents=True, exist_ok=True)
    total = 0

    # 1. The concepts themselves. `index.md` is skipped here because it is that
    #    directory's navigation page and is written in step 3.
    for page in pages:
        if page.rel_path == INDEX_FILENAME or page.rel_path.endswith("/" + INDEX_FILENAME):
            continue
        target = bundle_dir / page.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = page.rendered or render_concept(page)
        target.write_text(rendered, encoding="utf-8")
        total += len(rendered.encode("utf-8"))

    # 2. The navigation tree, built from the pages rather than from whatever
    #    directories happened to be visited.
    landing: dict[str, Page] = {}
    tree: dict[str, dict[str, set | list]] = {"": {"dirs": set(), "pages": []}}

    for page in pages:
        parts = page.rel_path.split("/")
        directory = "/".join(parts[:-1])
        if parts[-1] == INDEX_FILENAME:
            landing[directory] = page
            continue
        for depth in range(len(parts)):
            tree.setdefault("/".join(parts[:depth]), {"dirs": set(), "pages": []})
        tree[directory]["pages"].append((page.title, parts[-1]))  # type: ignore[union-attr]
        for depth in range(len(parts) - 1):
            tree["/".join(parts[:depth])]["dirs"].add(parts[depth])  # type: ignore[union-attr]

    # A directory that exists only because a landing page lives in it still needs
    # a node, and still needs to appear in its parent's listing.
    for directory in landing:
        parts = directory.split("/") if directory else []
        for depth in range(len(parts) + 1):
            tree.setdefault("/".join(parts[:depth]), {"dirs": set(), "pages": []})
        for depth in range(len(parts)):
            tree["/".join(parts[:depth])]["dirs"].add(parts[depth])  # type: ignore[union-attr]

    # 3. One navigation page per directory. Where the source has its own landing
    #    page, that page *is* the navigation, so its content is kept and the
    #    generated sections are appended underneath it.
    for directory, node in tree.items():
        subdirectories = sorted(node["dirs"])  # type: ignore[arg-type]
        children = sorted(node["pages"], key=lambda item: item[1])  # type: ignore[arg-type]
        target = bundle_dir / directory / INDEX_FILENAME if directory else bundle_dir / INDEX_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)

        existing = landing.get(directory)
        if existing is None:
            rendered = index_for(directory, subdirectories, children)
        else:
            navigation = navigation_body(subdirectories, children)
            body = existing.body.rstrip()
            rendered = render_concept(
                dataclasses.replace(existing, body=f"{body}\n\n{navigation}\n" if navigation else body)
            )
        target.write_text(rendered, encoding="utf-8")
        total += len(rendered.encode("utf-8"))
    return total


def concept_text(page: Page) -> str:
    """What gets embedded for one concept: the same fields the keyword index ranks."""
    parts = [page.title, page.description, " ".join(page.tags)]
    body = re.sub(r"```.*?```", " ", page.body, flags=re.DOTALL)
    parts.append(body[:EMBED_TEXT_CHARS])
    return "\n".join(part for part in parts if part).strip()


def embed_texts(
    endpoint: str, model: str, api_key: str, texts: list[str], batch: int, log
) -> list[list[float]]:
    """
    Embed a list of strings, batched.

    OpenAI-compatible on purpose: `/v1/embeddings` with `{model, input}` and
    `{data: [{embedding}]}` is what every hosted and self-hosted option speaks, so
    this is a contract rather than a vendor choice.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), max(1, batch)):
        window = texts[start : start + max(1, batch)]
        payload = json.dumps({"model": model, "input": window}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"the embedding endpoint answered {error.code}: {detail}") from error
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"the embedding endpoint could not be reached: {error}") from error
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(window):
            raise RuntimeError(
                f"the embedding endpoint returned {len(rows) if isinstance(rows, list) else 'no'} "
                f"vector(s) for {len(window)} input(s)"
            )
        for row in rows:
            vector = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(vector, list) or not all(isinstance(v, (int, float)) for v in vector):
                raise RuntimeError("the embedding endpoint returned a malformed vector")
            vectors.append([float(v) for v in vector])
        log(f"    embedded {min(start + len(window), len(texts))}/{len(texts)}")
    return vectors


def write_embeddings(
    root: Path, pages: list[Page], vectors: list[list[float]], model: str, log, complete: bool = True
) -> None:
    """
    Write the vector index beside the corpus.

    A ragged matrix is refused rather than padded: padding would silently make two
    vectors incomparable and every similarity score after them meaningless, and the
    plugin cannot tell a padded row from a real one.
    """
    import struct

    if not vectors:
        return
    dim = len(vectors[0])
    for index, vector in enumerate(vectors):
        if len(vector) != dim:
            raise RuntimeError(
                f"the embedding endpoint returned {len(vector)} dimensions for concept {index} "
                f"and {dim} for the first; a ragged matrix cannot be searched"
            )
    flat = [value for vector in vectors for value in vector]
    (root / EMBEDDINGS_BIN).write_bytes(struct.pack(f"<{len(flat)}f", *flat))
    (root / EMBEDDINGS_JSON).write_text(
        json.dumps(
            {
                "schema": EMBEDDINGS_SCHEMA,
                "model": model,
                "dim": dim,
                "count": len(vectors),
                "concept_ids": [f"{page.bundle}/{page.rel_path}" for page in pages],
                # Scope, declared. A build that carried bundles over from a previous
                # run has no vectors for them, so the index is partial — and an agent
                # asking a question those bundles answer would get keyword-only
                # ranking. Saying so is what lets the plugin report it instead of
                # implying the whole corpus is searchable semantically.
                "complete": complete,
                "bundles": sorted({page.bundle for page in pages}),
                "text_chars": EMBED_TEXT_CHARS,
                "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"    wrote {EMBEDDINGS_JSON} ({len(vectors)} × {dim})")


def local_root(source: Source, local_root_dir: Path) -> Path:
    """Where a `kind: local` folder is, whether it was declared absolute or not."""
    folder = Path(source.folder).expanduser()
    return folder if folder.is_absolute() else (local_root_dir / folder).resolve()


def newest_mtime(root: Path) -> str:
    """The newest mtime under a folder, as a UTC `Z` timestamp."""
    newest = 0.0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    if newest == 0.0:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.datetime.fromtimestamp(newest, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_source(source, workdir, log, limit=0, local_root_dir: Path | None = None):
    """Fetch and convert one source. Never raises: failures become results."""
    started = time.monotonic()
    result = SourceResult(name=source.name, ok=False)
    warnings: list[str] = []
    local_root_dir = local_root_dir or Path.cwd()
    try:
        if source.kind in ("git", "wiki"):
            if not source.repo:
                raise RuntimeError("no repo declared")
            log(f"  {source.name}: {source.repo}")
            checkout, commit, date, pinned = git_clone(source, workdir, log)
            result.commit, result.commit_date, result.pinned = commit, date, pinned
            result.upstream_age_days = age_in_days(date)
            result.ref = source.ref or "default"
            # The commit date becomes every concept's timestamp: `sources` then
            # reports the age of the *documentation*, which is the number an agent
            # needs to judge whether an answer is still true.
            pages, counts, collisions = build_pages(
                source, checkout, log, limit, timestamp_default=date, warnings=warnings
            )
        elif source.kind == "local":
            root = local_root(source, local_root_dir)
            if not root.is_dir():
                raise RuntimeError(
                    f"the declared folder does not exist: {root}. "
                    "A local source is a path on this host; relative paths resolve "
                    "against --local-root."
                )
            log(f"  {source.name}: {root}")
            # A folder's own newest mtime is its snapshot date. Build time would
            # report every local bundle as fresh on every run, which is the one
            # thing the age is supposed to tell an agent.
            newest = newest_mtime(root)
            result.commit_date = newest
            pages, counts, collisions = build_pages(
                source, root, log, limit, timestamp_default=newest, warnings=warnings
            )
        elif source.kind == "llms":
            log(f"  {source.name}: {source.url}")
            result.commit_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            pages, counts, collisions = build_pages(source, None, log, limit, warnings=warnings)
        else:
            raise RuntimeError(
                f"unknown kind '{source.kind}'; this builder understands: {', '.join(SOURCE_KINDS)}"
            )
        if not pages:
            # A source whose globs now match nothing used to report success and
            # overwrite its bundle with an index-only stub. That is the drift case
            # this design exists for, so it must fail and keep the previous bundle.
            raise RuntimeError(
                f"no pages matched ({counts.total} file(s) seen and filtered out). "
                "Check include/exclude globs, the declared path, and the ref — an "
                "empty result is treated as a failure so the previous bundle survives."
            )
        for page in pages:
            page.rendered = render_concept(page)
        result.warnings = list(warnings)
        result.pages_data = pages
        result.pages = len(pages)
        result.bytes = sum(len(page.rendered.encode("utf-8")) for page in pages)
        result.counts = counts
        result.collisions = collisions
        result.ok = True
    except Exception as error:  # a broken source must not cost the other thirteen
        result.ok = False
        result.error = str(error).strip().splitlines()[0][:300]
    result.seconds = round(time.monotonic() - started, 1)
    return result


def swap_into_place(staging: Path, root: Path, keep_previous: bool, log) -> None:
    """
    Move the finished corpus into place, keeping one previous copy.

    An agent may be mid-read while this runs, so the corpus is never mutated in
    place: the new tree is built beside it and renamed over. There is a
    microsecond window between the two renames where the root does not exist; the
    alternative — copying files into the live tree — is a much longer window with
    a torn corpus at the end of it.
    """
    previous = root.parent / (root.name + ".previous")
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists():
        if keep_previous:
            shutil.rmtree(previous, ignore_errors=True)
            os.replace(root, previous)
            log(f"  previous corpus kept at {previous}")
        else:
            shutil.rmtree(root)
    os.replace(staging, root)


def load_sources(path: Path) -> tuple[list[Source], list[str], dict]:
    """The sources, the global excludes, and the defaults they were built from."""
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = config.get("defaults") or {}
    global_exclude = [str(p) for p in (config.get("global_exclude") or [])]
    sources = []
    for name, raw in (config.get("sources") or {}).items():
        sources.append(Source.from_config(name, raw, defaults, global_exclude))
    return sources, global_exclude, defaults


def check_sources(sources: list[Source], local_root_dir: Path | None = None) -> int:
    """Resolve every source without building: what exists, and where it points."""
    failures = 0
    for source in sources:
        label = f"{source.name:<14}"
        try:
            if source.kind in ("git", "wiki"):
                # ls-remote on a pinned ref proves the ref exists; otherwise the
                # repository is at least reachable. Fetching whole trees to check
                # would make --check as slow as the build it is meant to precede.
                out = run(["git", "ls-remote", source.repo, source.ref or "HEAD"], timeout=90)
                if not out.split():
                    # git exits 0 with no output for a ref that does not exist, so
                    # a typo'd pin used to print `ok … ?` and fail silently later.
                    raise RuntimeError(f"ref '{source.ref}' does not exist in {source.repo}")
                sha = out.split()[0]
                pin = source.ref or "default branch"
                print(f"  ok    {label} {pin:<18} {sha[:12]}  {source.repo}")
            elif source.kind == "local":
                root = local_root(source, local_root_dir or Path.cwd())
                if not root.is_dir():
                    raise RuntimeError(f"the declared folder does not exist: {root}")
                count = sum(1 for p in root.rglob("*") if p.is_file())
                print(f"  ok    {label} local              {count:>6} files  {root}")
            elif source.kind == "llms":
                text, url, warning = fetch_llms(source, lambda _msg: None)
                if not text:
                    raise RuntimeError(warning)
                print(f"  ok    {label} {url} ({len(text) // 1024} KB)")
            else:
                raise RuntimeError(f"unknown kind '{source.kind}'")
        except Exception as error:
            failures += 1
            print(f"  FAIL  {label} {str(error).splitlines()[0][:90]}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an OKF documentation corpus.")
    parser.add_argument("--sources", default=str(Path(__file__).with_name("sources.yaml")))
    parser.add_argument("--out", default="./out/okf-bundles", help="corpus root to write")
    parser.add_argument("--work", default="./work", help="checkout cache")
    parser.add_argument("--bundle", action="append", default=[], help="only these sources")
    parser.add_argument("--limit-pages", type=int, default=0)
    parser.add_argument("--check", action="store_true", help="resolve sources, write nothing")
    parser.add_argument("--no-previous", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument(
        "--embed-endpoint",
        default="",
        help="OpenAI-compatible embeddings URL; enables the optional vector index",
    )
    parser.add_argument("--embed-model", default="", help="model name to send")
    parser.add_argument("--embed-batch", type=int, default=64)
    parser.add_argument(
        "--local-root",
        default=".",
        help="where a `kind: local` source's relative folder resolves (default: the working directory)",
    )
    args = parser.parse_args(argv)

    sources, global_exclude, defaults = load_sources(Path(args.sources))
    all_sources = list(sources)  # before --bundle narrowing; needed for carry-over
    if args.bundle:
        wanted = set(args.bundle)
        sources = [s for s in sources if s.name in wanted]
        missing = wanted - {s.name for s in sources}
        if missing:
            print(f"unknown bundle(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not sources:
        print("no sources selected", file=sys.stderr)
        return 2

    if args.check:
        print(f"checking {len(sources)} source(s)")
        return 1 if check_sources(sources, Path(args.local_root).expanduser().resolve()) else 0

    out_root = Path(args.out).resolve()
    workdir = Path(args.work).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Staging lives beside the corpus root so the final swap is a rename inside one
    # filesystem, which is the only kind that is atomic.
    out_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(out_root.parent)))

    # Read before the swap replaces the corpus this describes.
    previous_manifest_sources: dict[str, dict] = {}
    previous_manifest = out_root / MANIFEST_FILENAME
    if previous_manifest.is_file():
        try:
            previous_manifest_sources = (json.loads(previous_manifest.read_text()) or {}).get(
                "sources", {}
            )
        except (OSError, json.JSONDecodeError):
            previous_manifest_sources = {}

    started = time.monotonic()
    print(f"building {len(sources)} source(s) into {out_root}")

    def log(message: str) -> None:
        print(message, flush=True)

    results: list[SourceResult] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            local_root_dir = Path(args.local_root).expanduser().resolve()
            futures = {
                pool.submit(build_source, s, workdir, log, args.limit_pages, local_root_dir): s
                for s in sources
            }
            for future in concurrent.futures.as_completed(futures):
                source = futures[future]
                result = future.result()
                results.append(result)
                if result.ok:
                    log(
                        f"  done  {source.name:<14} {result.pages:>6} pages  "
                        f"{result.bytes / 1024:>8.0f} KB  {result.seconds}s"
                        + (f"  ({result.counts.total} filtered)" if result.counts.total else "")
                    )
                else:
                    log(f"  FAIL  {source.name:<14} {result.error}")

        results.sort(key=lambda r: r.name)
        manifest_sources: dict[str, dict] = {}
        stale: list[str] = []
        stale_upstreams: list[str] = []
        built_pages = 0
        built_bundles = 0
        totals = {"bundles": 0, "pages": 0, "bytes": 0}

        for result in results:
            source = next(s for s in sources if s.name == result.name)
            entry: dict = {
                "title": source.title,
                "repo": source.repo or source.url,
                "ref": result.ref,
                "pinned": result.pinned,
                "commit": result.commit,
                "commit_date": result.commit_date,
                "upstream_age_days": result.upstream_age_days,
                "archived": source.archived,
                "note": source.note,
                "pages": result.pages,
                "bytes": result.bytes,
                "filters": result.counts.as_dict(),
                "collisions": result.collisions,
                "stale": False,
                "error": None,
            }
            if result.upstream_age_days is not None and result.upstream_age_days > UPSTREAM_STALE_DAYS:
                entry["upstream_stale"] = not source.archived
                if source.archived:
                    log(
                        f"  note  {result.name:<14} upstream is archived; last commit "
                        f"{result.upstream_age_days} days ago. {source.note}".rstrip()
                    )
                else:
                    log(
                        f"  note  {result.name:<14} upstream has not moved in "
                        f"{result.upstream_age_days} days — check whether it relocates"
                    )
                    stale_upstreams.append(result.name)
            if result.ok:
                write_bundle(staging, source, result.pages_data)
                totals["bundles"] += 1
                built_bundles += 1
                built_pages += result.pages
                totals["pages"] += result.pages
                totals["bytes"] += result.bytes
                for warning in result.warnings:
                    entry.setdefault("warnings", []).append(warning)
            else:
                # Keep whatever the last successful build produced, and say so.
                previous_bundle = out_root / result.name
                if previous_bundle.is_dir():
                    shutil.copytree(previous_bundle, staging / result.name)
                    # Keep the provenance of the bundle that is actually being
                    # served. Building the entry fresh left a kept bundle claiming
                    # commit "" and 0 pages — the opposite of the point.
                    kept = previous_manifest_sources.get(result.name) or {}
                    for key in ("commit", "commit_date", "ref", "pinned", "pages", "bytes"):
                        if kept.get(key) not in (None, ""):
                            entry[key] = kept[key]
                    entry["stale"] = True
                    entry["error"] = result.error
                    stale.append(result.name)
                    totals["pages"] += int(kept.get("pages") or 0)
                    totals["bytes"] += int(kept.get("bytes") or 0)
                    log(
                        f"  stale {result.name:<14} kept the previous bundle from "
                        f"{str(kept.get('commit') or '?')[:8]} ({result.error})"
                    )
                else:
                    entry["error"] = result.error
            manifest_sources[result.name] = entry

        # A partial build must not silently delete the bundles it did not select.
        # `--bundle n8n` used to write a corpus containing only n8n, which is data
        # loss dressed up as a build flag. Unselected bundles are carried over, and
        # their previous manifest entries with them, so a rebuild of one source
        # costs one source of fetching rather than all fourteen.
        selected_names = {source.name for source in sources}
        carried: list[str] = []
        for source in all_sources:
            if source.name in selected_names:
                continue
            bundle = out_root / source.name
            if not bundle.is_dir():
                continue
            shutil.copytree(bundle, staging / source.name)
            carried.append(source.name)
            entry = previous_manifest_sources.get(source.name) or {
                "title": source.title,
                "repo": source.repo or source.url,
                "pages": None,
                "bytes": None,
                "stale": False,
                "error": None,
            }
            entry = {**entry, "carried_over": True}
            entry["stale"] = bool(entry.get("stale"))
            manifest_sources[source.name] = entry
            # Counted in the corpus, not in "built this run": the summary line
            # answers what this invocation did, the manifest describes what exists.
            totals["bundles"] += 1
            totals["pages"] += int(entry.get("pages") or 0)
            totals["bytes"] += int(entry.get("bytes") or 0)
        if carried:
            log(f"  kept  {len(carried)} unselected bundle(s): {', '.join(sorted(carried))}")

        # The optional vector index. Built from the bundles produced *this* run, so
        # a partial index says so rather than quietly ranking some bundles
        # semantically and others not at all.
        embeddings_note: dict = {}
        bundled_pages = [page for result in results if result.ok for page in result.pages_data]
        if args.embed_endpoint:
            if not bundled_pages:
                embeddings_note = {"error": "no bundles were built, so there was nothing to embed"}
            else:
                log(f"  embedding {len(bundled_pages)} concept(s) with {args.embed_model}")
                try:
                    vectors = embed_texts(
                        args.embed_endpoint,
                        args.embed_model,
                        os.environ.get("PAPERCLIP_DOCS_EMBED_KEY", ""),
                        [concept_text(page) for page in bundled_pages],
                        args.embed_batch,
                        log,
                    )
                    write_embeddings(
                        staging, bundled_pages, vectors, args.embed_model, log, complete=not carried
                    )
                    embeddings_note = {
                        "model": args.embed_model,
                        "dim": len(vectors[0]),
                        "count": len(vectors),
                        "complete": not carried,
                        "bundles": sorted({page.bundle for page in bundled_pages}),
                    }
                    if carried:
                        log(
                            f"  note  the vector index covers {len(embeddings_note['bundles'])} "
                            f"bundle(s); {len(carried)} carried-over bundle(s) are keyword-only"
                        )
                except RuntimeError as error:
                    # The corpus is valid without an index. Throwing it away over an
                    # optional extra would be the wrong trade — but saying nothing
                    # would leave an operator believing semantic search is on.
                    embeddings_note = {"error": str(error)}
                    log(f"  note  no vector index: {error}")

        manifest = {
            "okf_version": OKF_VERSION,
            "builder": "paperclip-docs-builder",
            "builder_version": BUILDER_VERSION,
            "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": manifest_sources,
            "totals": {
                **totals,
                "stale_bundles": sorted(stale),
                "stale_upstreams": sorted(stale_upstreams),
                "carried_over": sorted(carried),
            },
            "embeddings": embeddings_note,
            "filters": {
                "global_exclude": global_exclude,
                # The configured defaults, not literals. Hardcoding these made the
                # manifest state a policy that no longer matched the config.
                "drop_locales_default": bool(defaults.get("drop_locales", True)),
                "max_file_bytes_default": int(defaults.get("max_file_bytes", 262144)),
                "convert_default": defaults.get("convert", "auto"),
            },
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        swap_into_place(staging, out_root, not args.no_previous, log)
    finally:
        # Staging never survives a run, successful or not: a half-written corpus
        # left on disk is worse than none, because it looks buildable.
        shutil.rmtree(staging, ignore_errors=True)

    elapsed = round(time.monotonic() - started, 1)
    carried_note = f" + {len(carried)} carried over" if carried else ""
    print(
        f"built {built_bundles}/{len(sources)} bundles · {built_pages} pages this run"
        f"{carried_note} · corpus now {totals['pages']} pages, "
        f"{totals['bytes'] / 1024 / 1024:.1f} MB in {elapsed}s"
    )
    if stale:
        print(f"stale bundles kept from a previous build: {', '.join(sorted(stale))}")
    print(f"manifest: {out_root / MANIFEST_FILENAME}")
    return 0 if totals["bundles"] or stale else 1


if __name__ == "__main__":
    sys.exit(main())
