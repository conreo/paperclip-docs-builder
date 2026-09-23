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

| Source | Kind | Note |
|---|---|---|
| n8n | git | 1,332 markdown pages, no conversion |
| grafana, loki | git | `docs/sources`, path-filtered out of a large repository |
| rocketchat | git | markdown |
| authentik, zulip | git | MDX: imports and JSX tags removed, prose kept |
| nextcloud | git (rst) | 529 RST files, converted with pandoc |
| borg, restic | git (rst) | pandoc |
| vaultwarden, uptime-kuma | wiki | the `.wiki.git` repository |
| brevo | llms | `llms-full.txt`, split per section |
| line | llms | `llms.txt` |
| erpnext | git | **archived upstream (2021)** — see below |

RST is the only format that needs an external tool. `pandoc` is a binary, not a
pip package:

```bash
# Debian/Ubuntu
apt-get install -y pandoc
# anywhere, without root: the static release binary
curl -sL https://github.com/jgm/pandoc/releases/latest/download/pandoc-3.11-linux-amd64.tar.gz \
  | tar xz -C .tools --strip-components=1
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

Then point the plugin's `corpusRoot` at `/paperclip/offline-docs/okf-bundles`, and
refresh on a schedule with the same command.

## Options

| Flag | Meaning |
|---|---|
| `--check` | resolve every source and report where it points; writes nothing |
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

34 tests over the pure parts: glob matching, filters, MDX and GitBook stripping,
frontmatter rendering, title/type/resource derivation, index generation, the
staging swap, and config validation. Three of them are regressions for bugs the
first real build exposed — an exclude that matched a directory but not its
contents, a bundle that got no root index because all its pages were nested, and a
declared `path` leaking into the output layout. Each produced a corpus that looked
fine.

## Requirements

- Python ≥ 3.11 (uses `tomllib`-era typing, f-strings, `dataclasses`)
- `pyyaml`
- `git`
- `pandoc` — only for `convert: rst`

## Licence

The builder is MIT. The documentation it fetches belongs to its upstream
projects, each with its own terms; redistributing a corpus is between you and
them. That is why the `paperclip-docs` plugin ships no corpus and this repository
ships no built output.
