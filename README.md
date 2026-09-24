# paperclip-docs-builder

Build an OKF documentation corpus from declared sources, for the
[`paperclip-docs`](https://github.com/conreo/paperclip-docs) Paperclip plugin to
serve.

One pipeline, declarative adapters. Every source goes through the same stages —
fetch, filter, convert, frontmatter, write, manifest — and the only thing that
varies per source is an entry in `sources.yaml`.

```bash
./fetch.py --check                       # resolve every source, write nothing
./fetch.py --bundle n8n --bundle grafana  # build just these
./fetch.py                               # everything, into ./out/okf-bundles
```

## Why it is shaped this way

The pipeline this replaces turned each documentation source into **code**. It grew
a 20 KB `fix_docs.py` of per-site patches, and its acquisition stages can no longer
be re-run at all — the corpus that exists today cannot be rebuilt from its own
repository. That is the failure mode this design exists to avoid, so:

- **Adding a source is data.** A glob, a converter name, a resource base. If a
  source needs something the schema cannot express, the schema is wrong.
- **Reproducible.** Each source records the commit it was built from and that
  commit's date, so a citation can be traced and a rebuild compared. An unpinned
  source follows the default branch and is recorded as unpinned.
- **Degrading, not failing.** One broken source must not cost you the other
  thirteen. A source that fails keeps its previous bundle and is marked `stale` in
  the manifest, which the plugin reports to the agent.
- **Honest about age.** Every concept carries its source's snapshot date, and a
  source whose upstream has not moved in a year is reported. A corpus that quietly
  stops updating is the ordinary way this rots.

## What it produces

```
out/okf-bundles/
  manifest.json                 which revision each bundle came from, and when
  n8n/                          one bundle per source
    index.md                    navigation for the directory
    administer/
      index.md
      README.md                 a concept: markdown + OKF frontmatter
```

Each concept carries frontmatter the plugin's parser accepts — flat scalars and
sequences only, everything quoted, because its parser strips ` #` as a comment and
reads a leading `[` as a sequence:

```markdown
---
type: "Administer"
title: "Administer"
description: "Secure, manage, and operate your n8n instance."
resource: "https://docs.n8n.io/administer"
tags: ["n8n"]
timestamp: "2026-09-23T08:03:10Z"
okf_version: "0.1"
---
```

`timestamp` is the **source's** commit date, not the build time, so the plugin's
`sources` tool reports how old the documentation is rather than how recently you
ran the builder.

`manifest.json` is the file the plugin looks for (it also accepts
`okf-manifest.json` and `build-manifest.json`). It is reported verbatim by the
`sources` tool, so an agent can see that its n8n answer came from revision
`fc0ada51` dated 2026-09-23 — and that the ERPNext bundle is a 2021 snapshot whose
upstream is archived.

## Sources

Three kinds — `git`, `wiki`, `llms` — and a `convert:` field per source: `auto`
strips MDX syntax, `rst` runs pandoc, `none` copies markdown verbatim.

| Source | Kind | Convert | Note |
|---|---|---|---|
| n8n | git | auto | 1,506 markdown files in the repository, ~1,330 pages after filtering |
| grafana, loki | git | auto | `docs/sources`, path-filtered out of a large repository |
| rocketchat | git | auto | markdown |
| authentik, zulip | git | auto | MDX: imports and JSX tags removed, prose kept |
| nextcloud | git | rst | 529 RST files |
| borg, restic | git | rst | |
| vaultwarden, uptime-kuma | wiki | auto | the `.wiki.git` repository |
| brevo | llms | — | `llms-full.txt`, split per section |
| line | llms | — | `llms.txt` |
| erpnext | git | auto | **archived upstream (2021)** — see below |

RST is the only format that needs an external tool. `pandoc` is a binary, not a
pip package:

```bash
# Debian/Ubuntu
apt-get install -y pandoc

# Without root: the static release binary. `latest/download/` cannot be combined
# with a versioned filename, so pin the tag — and pandoc is looked up on PATH, so
# extract it into one rather than into ./.tools.
PANDOC_VERSION=3.11
curl -sL "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-amd64.tar.gz" \
  | tar xz --strip-components=2 -C /usr/local/bin "pandoc-${PANDOC_VERSION}/bin/pandoc"
```

Sources that declare `convert: rst` fail loudly without it rather than writing
half-converted pages.

## What the filters are for

The point of owning the pipeline is deciding what *does not* go in the corpus:

| Filter | Why |
|---|---|
| `include` / `exclude` globs | drop changelogs, release notes, contributor guides. An exclude also removes everything *under* a match — `**/changelog*` takes `docs/changelog/v1.md` with it |
| `drop_locales` | keep English only; translations can double a bundle |
| `max_file_bytes` | skip link farms and generated dumps (default 256 KB) |
| `max_pages` | a hard cap per bundle when a source is larger than you want |
| `ref` | pin the version each organization runs, instead of following the default branch |

Every drop is counted **by reason** in the manifest (`by_glob`, `by_locale`,
`oversized`, `unreadable`, `empty`) — a corpus that cannot say what it excluded
cannot be tuned.

## Honouring a request from the plugin

The `paperclip-docs` plugin cannot build: the runtime gives it no way to spawn a
process. It writes a small JSON *request* instead — which sources, at which
versions — into a folder the operator declared, and this runner is the other half:

```bash
./runner.py --out /paperclip/offline-docs/okf-bundles --once      # cron or a systemd timer
./runner.py --out /paperclip/offline-docs/okf-bundles --watch 60  # or poll
```

`--requests` is derived from `--out` (as `<out>.requests`) because that is where the plugin writes
it. Both halves derive the same directory, so neither has to be told a path — and nothing about the
deployment ends up in a settings page.

`--once` is for cron or a systemd timer; `--watch` polls. After each request it
writes `response.json` beside it, which the plugin reads, so the operator sees the
outcome without reading container logs. Requests are renamed rather than deleted —
`.done` or `.failed` — because an operator chasing a failure needs the document that
caused it.

A request is *refused by name* when it cannot be honoured (a schema from a newer
plugin, an unknown source kind, no sources). Refusing loudly matters: a silently
ignored request looks exactly like a build that found nothing to do. A request whose
corpus was already built afterwards is **skipped**, which is what makes a cron and a
settings button safe to race.

## A project's own documentation

`kind: local` folds a folder from this host into the corpus as a bundle — the same
idea as the reference provisioning system's `LOCAL_DOCS`. Relative folders resolve
against `--local-root`:

```yaml
handbook:
  kind: local
  title: Our handbook
  folder: docs          # absolute, or relative to --local-root
```

Its snapshot date is the folder's own newest mtime, not the build time: build time
would report a local bundle as fresh on every run, which is the one thing the age is
supposed to tell an agent.

## Conformance: `lint.py`

The corpus targets **OKF v0.2**, the specification published in
[`GoogleCloudPlatform/knowledge-catalog/okf`](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf).
Conformance is a property of files, so checking it needs no model and costs no tokens:

```bash
python3 lint.py out/okf-bundles            # human-readable
python3 lint.py out/okf-bundles --json     # machine-readable
python3 lint.py out/okf-bundles --strict   # a warning fails too
```

Every rule quotes the section of the spec it comes from. Errors are spec violations —
a concept with no `type` (§4.1), frontmatter in an `index.md` (§8), `okf_version`
outside the bundle root (§12), an `index.md` used as a concept document (§3.1), a
`sources` entry with no `resource` (§5.1), `generated` with no `by` (§5.2), a timestamp
with no explicit offset (§5). Warnings are the quality rules the spec states as SHOULD
plus hygiene it does not cover at all: markup indexed as prose, undecoded entities,
binaries read as text, stubs, listings with no descriptions.

**Every build lints itself.** The result goes into `manifest.json` under `lint`, and
`--lint-strict` fails the build on errors. Strict is opt-in because a builder that
refuses to produce a corpus until it is perfect leaves an operator with no corpus; the
default says so loudly and keeps building.

The linter is also how the four divergences found here were found — 5,366 of them, all
now zero:

| Was | Now |
|---|---|
| 4,316 × `okf_version` on every concept | declared once, or not at all |
| 3,795 × legacy `timestamp` | accepted (§13.1), migration pending |
| 525 × `index.md` carrying concept frontmatter | landing pages moved to `overview.md` |
| 525 × reserved filename used as a concept (§3.1) | every `index.md` is a generated listing |
| 753 × listings with no descriptions | entries carry the child's `description` |

## The optional vector index

Keyword search is the baseline and needs nothing. If you want semantic retrieval as
well, point the builder at an OpenAI-compatible embeddings endpoint (`/v1/embeddings`
with `{model, input}`); the key is read from `PAPERCLIP_DOCS_EMBED_KEY`.

```bash
PAPERCLIP_DOCS_EMBED_KEY=... ./fetch.py \
  --embed-endpoint https://api.example.com/v1/embeddings --embed-model bge-small
```

It writes two files beside the corpus — `embeddings.json` (schema, model, dimension,
concept ids, and whether the index is *complete*) and `embeddings.bin` (a flat
float32 matrix). No database, no native module: the plugin reads them with `node:fs`
and does the arithmetic itself, which is the only way a worker that cannot spawn
anything can search vectors.

A failed endpoint costs you the index, **not the corpus**: the build still succeeds
and the manifest records why there is no index. A ragged response is refused rather
than padded, because a padded vector is incomparable with the others and the plugin
cannot tell a padded row from a real one.

## Deployment

The builder should not run inside the Paperclip image: it needs Python, `git` and
`pandoc`, and the plugin's own container has Python but no `pip`. Run it as its own
container writing to the persistent volume the plugin is pointed at:

```bash
docker run --rm \
  -v "$PWD:/src" -w /src \
  -v /paperclip/offline-docs:/corpus \
  python:3.13-slim \
  sh -c 'apt-get update && apt-get install -y git pandoc && pip install pyyaml &&
         ./fetch.py --out /corpus/okf-bundles'
```

The corpus is swapped into place rather than written over: the new tree is built
beside the live one and renamed, so an agent mid-read never sees a half-written
corpus. One `okf-bundles.previous` is kept.

Then point the plugin's `corpusRoot` at `/paperclip/offline-docs/okf-bundles`.

### The plugin cannot run any of this

That one-shot command is fine for a first corpus, but the plugin cannot invoke it: its
worker has no way to spawn a process. It writes a *request* instead, and something on
the host has to honour it — `runner.py`, which polls `<corpus>.requests/` every 30 s and
writes `response.json` back.

That runner, the optional embedding server, and the tailnet route the plugin's outbound
guard requires are all in **[`deploy/`](deploy/README.md)**. Read it before operating
this: it has the measured timings, the path-namespace difference between a containerised
plugin and a host runner, the flags that fail in confusing ways, and a troubleshooting
table.

## Options

| Flag | Meaning |
|---|---|
| `--check` | resolve every source and report where it points; writes nothing. A ref that does not exist fails rather than printing `ok` |
| `--sources FILE` | the config to read (default: `sources.yaml` beside the script) |
| `--bundle NAME` | build only these sources (repeatable) |
| `--out DIR` | corpus root to write (default `./out/okf-bundles`) |
| `--work DIR` | checkout cache, reused across runs (default `./work`) |
| `--limit-pages N` | per-source cap, for a smoke run |
| `--jobs N` | sources fetched concurrently (default 4) |
| `--no-previous` | do not keep the previous corpus |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

59 tests. The unit tests cover the pure parts: glob matching, filters, MDX and
GitBook stripping, frontmatter rendering, title/type/resource derivation, index
generation, the staging swap, and config validation.

`tests/test_degradation.py` drives `main()` against real local git repositories, so
it covers what only appears in a whole build: a source that stops matching keeps its
previous bundle and is marked stale with its original provenance; an empty first
build fails instead of shipping a stub; changing `repo` rebuilds from the new one
rather than reusing the cached checkout; and `--check` fails on a ref that does not
exist.

Most of these are regressions for bugs a passing unit suite did not catch: an
exclude that matched a directory but not its contents; a bundle with no root index
because all its pages were nested; a declared `path` leaking into the output layout;
525 source landing pages overwritten by generated navigation while still being
counted; and a source matching nothing silently replacing a good bundle with an empty
stub. Each produced a corpus that looked fine.

## Requirements

- Python ≥ 3.9. Every annotation is lazy (`from __future__ import annotations`) and
  no builtin generic is subscripted at runtime. Verified on 3.14; the container
  image uses 3.13.
- `pyyaml`
- `git`
- `pandoc` — only for `convert: rst`

## Licence

The builder is MIT. The documentation it fetches belongs to its upstream
projects, each with its own terms; redistributing a corpus is between you and
them. That is why the `paperclip-docs` plugin ships no corpus and this repository
ships no built output.
