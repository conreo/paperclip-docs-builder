#!/usr/bin/env python3
"""
Honour a refresh request from the paperclip-docs plugin.

The plugin cannot build. The runtime gives it no way to spawn a process, so `git`
and `pandoc` are out of reach and a rebuild is not something it is able to do. What
it does instead is write a request — a small JSON document naming the sources it
wants, at the versions it wants them — into a folder the operator declared. This is
the other half: a process on the host that reads those requests and builds.

    ./runner.py --once          # honour a pending request, then exit (cron/systemd)
    ./runner.py --watch 60      # poll every 60 seconds

Why the split is worth the extra moving part:

  * **the corpus is a trust input.** An agent that can write it decides what every
    other agent believes. Agents can read this corpus; only this runner writes it,
    and only into the directory it was pointed at.
  * **builds stay reproducible.** This is a script with pinned refs and a
    checkout cache, not an agent run with a token and a timeout.
  * **no credentials in the worker.** The request names sources; it carries no keys.

What it does with a request
---------------------------

1. Read `<requests>/request.json`. The plugin writes it atomically, so a reader
   never sees a half-written file.
2. Refuse anything it does not understand — a request from a newer plugin, or one
   with an unknown source kind. Refusing loudly is the point: a silently ignored
   request looks exactly like a build that found nothing to do.
3. Skip a request that is already satisfied. If the corpus was built *after* the
   request was written, there is nothing to do — which matters when a cron and a
   button race, and is what makes a retry safe.
4. Build, by writing the request's sources to a temporary config and running the
   builder exactly as an operator would.
5. Write `<requests>/response.json` describing what happened, and clear the
   request. The plugin reads that response, so the operator sees the outcome
   without reading container logs.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch  # noqa: E402
import yaml  # noqa: E402

REQUEST_FILENAME = "request.json"
RESPONSE_FILENAME = "response.json"


def requests_dir_for(corpus_root: Path) -> Path:
    """
    Where the plugin writes its requests, derived from the corpus root.

    Both halves derive this the same way so that neither has to be *told* a path.
    The plugin's first version asked the operator to choose a directory for it,
    which is a deployment detail leaking into a settings page; a sibling of the
    corpus is implied by where the corpus already is.
    """
    return corpus_root.with_name(corpus_root.name + ".requests")
#: The schema this runner understands. A newer plugin must not be silently ignored.
SUPPORTED_SCHEMA = 1
#: The halves of the pipeline a request can ask for. `index` deliberately fetches
#: nothing, so it can run against a corpus whose sources are long gone.
MODES = ("okf", "index", "both")
#: Requests are renamed rather than deleted: an operator chasing a failure needs
#: the exact document that caused it.
DONE_SUFFIX = ".done"
FAILED_SUFFIX = ".failed"


@dataclasses.dataclass
class Outcome:
    """What the runner did, and why. Written to `response.json`."""

    status: str  # "built" | "skipped" | "refused" | "failed" | "idle"
    reason: str
    requested_at: str = ""
    finished_at: str = ""
    corpus_root: str = ""
    pages: int = 0
    bundles: int = 0
    #: What the vector index now holds, when the request asked for one. Its own field
    #: rather than part of `reason` because the settings page shows the counts.
    index: dict = dataclasses.field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            key: value
            for key, value in dataclasses.asdict(self).items()
            if value not in ("", 0) and value != {} and value != []
        }


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict | None:
    """Parse a JSON object, or None. Never raises: a bad file is data, not a crash."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def corpus_built_at(corpus_root: Path) -> str:
    """`built_at` from the build manifest, or "" when there is not one."""
    manifest = read_json(corpus_root / fetch.MANIFEST_FILENAME)
    if not manifest:
        return ""
    value = manifest.get("built_at")
    return value if isinstance(value, str) else ""


def request_mode(request: dict) -> str:
    """
    Which half of the pipeline a request asks for.

    `okf` builds the corpus and nothing else, `index` rebuilds the vector index from
    the corpus already on disk, `both` does the two in one pass.

    A request carrying an embed block but naming no mode is `both` — that is the
    shape the plugin sends once an operator has asked for semantic retrieval — and a
    request with neither is `okf`, which is what every request meant before modes
    existed. Defaulting the other way would make an old plugin's requests suddenly
    try to embed.
    """
    mode = request.get("mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()
    embed = request.get("embed")
    return "both" if isinstance(embed, dict) and embed else "okf"


def validate_request(request: dict) -> str:
    """Return a refusal reason, or "" when the request is usable."""
    schema = request.get("schema")
    if schema != SUPPORTED_SCHEMA:
        return (
            f"request schema {schema!r} is not supported by this runner "
            f"(it understands {SUPPORTED_SCHEMA}); upgrade the runner"
        )
    if not isinstance(request.get("corpusRoot"), str) or not request["corpusRoot"].strip():
        return "request has no corpusRoot"

    mode = request_mode(request)
    if mode not in MODES:
        return f"mode {mode!r} is not one this runner understands ({', '.join(MODES)})"

    if mode in ("index", "both"):
        embed = request.get("embed")
        if not isinstance(embed, dict):
            return f"mode {mode!r} needs an embed block naming an endpoint and a model"
        endpoint = embed.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            return "embed.endpoint is missing"
        if not re.match(r"^https?://", endpoint.strip()):
            return "embed.endpoint must be an http(s) URL"
        if not isinstance(embed.get("model"), str) or not embed["model"].strip():
            return "embed.model is missing"

    if mode == "index":
        # The corpus is already on disk, so no registry is involved — which is the
        # point. Rebuilding an index must not depend on the sources that built the
        # corpus still existing, still resolving, or still being declared.
        return ""

    sources = request.get("sources")
    if not isinstance(sources, list) or not sources:
        return "request declares no sources"
    known_kinds = set(fetch.SOURCE_KINDS)
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return f"sources[{index}] is not an object"
        if not source.get("id"):
            return f"sources[{index}] has no id"
        kind = source.get("kind")
        if kind not in known_kinds:
            # `local` is declared by the plugin before the builder implements it, so
            # this message is the one an operator will actually see. Name it.
            return f"sources[{index}].kind {kind!r} is not one this builder can fetch"
    return ""


def builder_config(sources: list[dict]) -> dict:
    """
    Translate the plugin's registry into the builder's config.

    The two shapes are deliberately similar but not identical: the plugin's entry is
    what an operator edits in a form, and the builder's is what the pipeline reads.
    Keeping the translation in one function means a drift between them is one
    visible place rather than a scattered condition.
    """
    out: dict[str, dict] = {}
    for source in sources:
        entry: dict = {"kind": source.get("kind", "git")}
        if source.get("title"):
            entry["title"] = source["title"]
        for key in ("repo", "url", "ref", "path"):
            if source.get(key):
                entry[key] = source[key]
        if source.get("convert"):
            entry["convert"] = source["convert"]
        for key in ("include", "exclude", "tags"):
            if source.get(key):
                entry[key] = list(source[key])
        out[str(source["id"])] = entry
    return out


def embed_args(embed: dict) -> list[str]:
    """The builder's embedding flags for one request."""
    return [
        "--embed-endpoint", str(embed.get("endpoint", "")).strip(),
        "--embed-model", str(embed.get("model", "")).strip(),
        "--embed-batch", str(int(embed.get("batch") or 64)),
    ]


def index_summary(root: Path) -> dict:
    """What the index at `root` now holds, for `response.json` — or {} when none."""
    summary = read_json(root / fetch.EMBEDDINGS_JSON) or {}
    return {key: summary[key] for key in ("model", "dim", "count", "complete") if key in summary}


def rebuild_index(root: Path, embed: dict, requested_at: str, log, note: str = "") -> Outcome:
    """
    `mode: index` — embed the corpus already on disk, fetching nothing.

    Separate from the build path on purpose: `--index-only` does not read the source
    registry at all, so an index can be rebuilt after sources have moved, changed
    shape, or been removed from the plugin's configuration.
    """
    log(f"  indexing {root}")
    code = fetch.main(["--index-only", "--out", str(root), *embed_args(embed)])
    if code != 0:
        return Outcome(
            status="failed",
            reason="the index build failed",
            requested_at=requested_at,
            finished_at=now_iso(),
            corpus_root=str(root),
            error=f"the builder exited {code}",
        )
    return Outcome(
        status="built",
        reason="index rebuilt" + (f" — {note}" if note else ""),
        requested_at=requested_at,
        finished_at=now_iso(),
        corpus_root=str(root),
        index=index_summary(root),
    )


def honour_request(requests: Path, corpus_root: Path, log) -> Outcome:
    """
    Do the work for one pending request.

    Returns the outcome; every path out of here is a described state rather than an
    exception, because a cron job that dies silently is worse than one that reports
    a refusal.
    """
    request_path = requests / REQUEST_FILENAME
    if not request_path.is_file():
        return Outcome(status="idle", reason="no request is waiting")

    request = read_json(request_path)
    finished = now_iso()
    if request is None:
        # A half-written or corrupt file: the plugin writes atomically so this means
        # something else put it there, and retrying it forever would spin.
        request_path.rename(request_path.with_name(request_path.name + FAILED_SUFFIX))
        return Outcome(
            status="refused", reason="the request is not readable JSON", finished_at=finished
        )

    requested_at = str(request.get("requestedAt") or "")
    refusal = validate_request(request)
    if refusal:
        request_path.rename(request_path.with_name(request_path.name + FAILED_SUFFIX))
        return Outcome(
            status="refused",
            reason=refusal,
            requested_at=requested_at,
            finished_at=finished,
        )

    # The request names where the corpus belongs; the runner's own --out is the
    # fallback so a misconfigured plugin cannot scatter corpora around the host.
    #
    # It is also the fix for a namespace difference. The plugin writes the path as
    # *its worker* sees it, and a plugin running in a container sees `/paperclip/…`
    # where the host has a volume path — so an absolute path the runner cannot
    # resolve is not a different corpus, it is the same corpus described from
    # somewhere else. The runner's --out is the operator's own statement of where the
    # corpus is, so that wins when the declared path is not there.
    declared_root = Path(str(request["corpusRoot"])).expanduser()
    relocated = ""
    if not declared_root.is_absolute():
        root = corpus_root
    elif declared_root.is_dir():
        root = declared_root
    else:
        root = corpus_root
        relocated = (
            f"the request names {declared_root}, which this host cannot see; "
            f"used {corpus_root}"
        )
        log(f"  note  {relocated}")

    mode = request_mode(request)
    embed = request.get("embed") if isinstance(request.get("embed"), dict) else {}

    if mode == "index":
        # No corpus build, so the "already built after this request" skip below does
        # not apply: the corpus being fresh says nothing about whether its index is.
        outcome = rebuild_index(root, embed, requested_at, log, relocated)
        suffix = DONE_SUFFIX if outcome.status == "built" else FAILED_SUFFIX
        request_path.rename(request_path.with_name(request_path.name + suffix))
        return outcome

    built_at = corpus_built_at(root)
    if built_at and requested_at and built_at >= requested_at:
        # Already newer than the request. A cron and a button can race, and this is
        # what makes honouring a request idempotent.
        request_path.rename(request_path.with_name(request_path.name + DONE_SUFFIX))
        return Outcome(
            status="skipped",
            reason=f"the corpus was already built at {built_at}, after this request",
            requested_at=requested_at,
            finished_at=finished,
            corpus_root=str(root),
        )

    sources = request["sources"]
    log(f"  building {len(sources)} source(s) into {root}")
    config = {"version": 1, "sources": builder_config(sources)}
    workdir = requests.parent / "work"
    with tempfile.TemporaryDirectory(prefix="runner-config-") as tmp:
        config_path = Path(tmp) / "sources.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        code = fetch.main(
            [
                "--sources", str(config_path),
                "--out", str(root),
                "--work", str(workdir),
                # One job at a time: this process may be one of several on a host,
                # and the builder's swap is atomic but the checkout cache is shared.
                "--jobs", "2",
                # Only when the request asked for both: a corpus rebuild writes its
                # index into staging so the two are promoted together, which is the
                # one ordering that cannot leave a corpus newer than its index.
                *(embed_args(embed) if mode == "both" else []),
            ]
        )

    manifest = read_json(root / fetch.MANIFEST_FILENAME) or {}
    totals = manifest.get("totals") if isinstance(manifest.get("totals"), dict) else {}
    stale = sorted((totals or {}).get("stale_bundles") or [])
    if code != 0 and not stale:
        request_path.rename(request_path.with_name(request_path.name + FAILED_SUFFIX))
        return Outcome(
            status="failed",
            reason="the build produced no bundles",
            requested_at=requested_at,
            finished_at=now_iso(),
            corpus_root=str(root),
        )

    request_path.rename(request_path.with_name(request_path.name + DONE_SUFFIX))
    return Outcome(
        status="built",
        reason=("built" if not stale else f"built; kept stale bundles: {', '.join(stale)}")
        + (f" — {relocated}" if relocated else ""),
        requested_at=requested_at,
        finished_at=now_iso(),
        corpus_root=str(root),
        pages=int((totals or {}).get("pages") or 0),
        bundles=int((totals or {}).get("bundles") or 0),
        # Reported whatever the mode: if a `both` request embedded the corpus, the
        # page should be able to say how many vectors the rebuild left behind.
        index=index_summary(root),
    )


def write_response(requests: Path, outcome: Outcome) -> None:
    """
    Publish the outcome where the plugin can read it.

    Written to a temporary file and renamed, for the same reason the request is: the
    plugin may read this at any moment and must never see half a document.
    """
    payload = json.dumps(outcome.as_dict(), indent=2, sort_keys=True) + "\n"
    target = requests / RESPONSE_FILENAME
    tmp = requests / (RESPONSE_FILENAME + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honour paperclip-docs refresh requests.")
    parser.add_argument("--out", default="./out/okf-bundles", help="the corpus root")
    parser.add_argument(
        "--requests",
        default="",
        help="override the request folder; by default it is derived from --out "
        "(<out>.requests), which is where the plugin writes",
    )
    parser.add_argument("--once", action="store_true", default=True, help="handle one request and exit")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="poll instead of exiting")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    corpus_root = Path(args.out).expanduser()
    # Derived unless overridden, so a deployment that points the plugin at a corpus
    # has already told the runner everything it needs.
    requests = (
        Path(args.requests).expanduser() if args.requests else requests_dir_for(corpus_root)
    )
    # An absent request folder is not an error: it means no one has asked for a
    # rebuild yet, which is the normal state of a corpus that is up to date.
    if not requests.is_dir():
        if not args.quiet:
            print(f"no request folder yet: {requests}")
        return 0

    def log(message: str) -> None:
        if not args.quiet:
            print(message, flush=True)

    def once() -> Outcome:
        outcome = honour_request(requests, corpus_root, log)
        if outcome.status != "idle":
            write_response(requests, outcome)
            log(f"  {outcome.status}: {outcome.reason}")
        return outcome

    if not args.watch:
        outcome = once()
        # Only a refused or failed request is an error for the caller: cron's exit
        # code should tell an operator that something needs attention, not that no
        # work was waiting.
        return 1 if outcome.status in ("refused", "failed") else 0

    log(f"watching {requests} every {args.watch}s")
    while True:
        try:
            once()
        except Exception as error:  # the loop must survive a bad night
            log(f"  error: {error}")
        time.sleep(max(5, args.watch))


if __name__ == "__main__":
    sys.exit(main())
