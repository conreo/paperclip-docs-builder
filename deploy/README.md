# Deploying the Docs pipeline

Three pieces, only one of which is the plugin:

| Piece | Runs | Why it is separate |
|---|---|---|
| **`paperclip-docs`** (the plugin) | inside Paperclip | serves the corpus as four read-only tools |
| the **runner** (`runner.py`) | a host process beside the corpus | the plugin's worker cannot spawn a process, and fetching needs `git` and `pandoc` |
| an **embedding server** (optional) | anywhere the runner *and* the plugin can both reach | semantic retrieval; keyword search needs none of it |

Keyword search works with the first two. Everything below about embeddings, GPUs and
proxies is one optional layer on top.

## 1. The runner

```bash
install -d /opt/paperclip-docs-builder
install -m 0644 fetch.py lint.py runner.py /opt/paperclip-docs-builder/
install -m 0644 deploy/paperclip-docs-runner.service /etc/systemd/system/
# edit --out in the unit to the corpus as *this host* sees it, then:
systemctl daemon-reload && systemctl enable --now paperclip-docs-runner
```

The runner polls `<corpus>.requests/request.json` every 30 s, performs what it asks,
writes `response.json`, and renames the request `.done` or `.failed`. Nothing else
needs to call it: the plugin's buttons only write requests.

**It runs as root**, and that is not an accident. The corpus usually lives in a Docker
volume — `/var/lib/docker/volumes/<volume>/_data/...` — and the parent directories are
mode 0700 root, so a non-root runner cannot even stat it. `Nice=10` keeps a 13,000-page
rebuild from being the reason the control plane feels slow.

### The path namespace trap

The plugin writes `corpusRoot` as **its worker** sees it — `/paperclip/offline-docs/okf-bundles`
inside a container — while the runner may be on the host, where that path does not
exist. The runner therefore does not trust the declared path: if it is not a directory
*here*, the runner uses its own `--out` and records why:

```
"reason": "index rebuilt — the request names /paperclip/…, which this host cannot see;
           used /var/lib/docker/volumes/…"
```

An earlier version failed the request instead, which turned a working index rebuild
into `index-only failed: no corpus at /paperclip/…`.

## 2. The embedding server (optional)

`deploy/docs-embed.container` is the whole thing:

```bash
MODELS=/home/rimko/models PORT=8081 ./deploy/docs-embed.container
```

It starts llama.cpp with `--embeddings`, every layer on the GPU, batch and context
sized for real pages. Two flags are not optional, and both fail in ways that look like
something else:

| Flag | Without it |
|---|---|
| `--embeddings` | `/v1/embeddings` answers **501** `"does not support embeddings. Start it with --embeddings"` |
| `-b 4096 -ub 4096` | pages longer than the 512-token default answer **500** `"input (665 tokens) is too large to process. increase the physical batch size (current batch size: 512)"` |

`--pooling cls` matches how bge-m3 was trained. Two servers with different pooling
produce vectors that are subtly incomparable — retrieval that is merely mediocre rather
than visibly broken, which is the hardest kind to notice.

Check it with one call. bge-m3 answers with 1024 floats:

```bash
curl -sS -X POST http://HOST:8081/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["single sign-on"]}' | head -c 120
```

### Timings, measured

For 13,029 concepts at the builder's 2,000-character budget:

| Where | Time |
|---|---|
| GTX 1080, `-ngl 99` | **~13 minutes** |
| 4 vCPU, no GPU | **~7 hours** |

Query-time embedding is ~55 ms for a short query through the tailnet proxy. The
character budget is `EMBED_TEXT_CHARS` in `fetch.py` (2,000) and is recorded in
`embeddings.json` as `text_chars`; the older ad-hoc indexer truncated at 256 characters,
which is ~7× faster to build and materially worse at matching a paraphrase.

## 3. Making it reachable — the outbound guard

**This is the trap that costs an afternoon.** The plugin's worker calls the endpoint
through `ctx.http.fetch`, which refuses private IPv4:

```
10/8   172.16/12   192.168/16   127/8   169.254/16   ::1   fc00::/7   fe80::/10
```

It does **not** block the tailnet range `100.64/10`. (The guard lives in the host's
`services/plugin-host-services.js` as `isPrivateIP`. A similarly-named
`remote-http-endpoint-guard.js` blocks `100.64/10` as well, but plugin fetches do not
go through it — check which one your version uses before designing around it.)

Consequences:

- **`rag.endpoint` must be an address the guard allows** — in practice a tailnet address.
- **The runner is a host process and is not guarded.** It may use a LAN address. So the
  index can be built against an address the plugin could never call; they only have to
  agree on the model and the dimension.
- If the embedder is on a LAN host and the plugin needs it too, forward it onto the
  tailnet — `deploy/docs-embed-proxy.service`, a single `socat` line:

```bash
socat TCP-LISTEN:8082,bind=<tailnet-ip>,fork,reuseaddr TCP:<embedder-lan-ip>:8081
```

A blocked endpoint **does not fail loudly**. Search degrades to keyword-only and says so
in a note, but the settings page still shows semantic retrieval as enabled. The only
check that exercises the guarded path is **Validate endpoint** on that page; a plain
`curl` from the host proves nothing about whether the *worker* can reach it.

Why private ranges are refused at all: otherwise a plugin could be pointed at any
service on the host's own network. The tailnet allowance is what makes a self-hosted
embedder possible.

## Where this deployment puts things

Written down because the topology has moved once already, and a runbook that describes
a different machine than the one running is worse than none:

| Piece | Runs on | Address |
|---|---|---|
| the plugin + the runner | the Paperclip VM | runner unit watches the corpus volume |
| `docs-embed` (bge-m3, `--embeddings -ngl 99`) | **the same VM** — the GTX 1080 is passed through to it (`hostpci0`) | publishes on the LAN: `192.168.1.222:8081` |
| the tailnet route (`socat`) | the same VM | `100.89.228.101:8082` → `192.168.1.222:8081` |
| `rag.endpoint` | plugin config | **`http://100.89.228.101:8082/v1/embeddings`** |

The GPU and the embeddings live next to the plugin, so the endpoint the plugin is
allowed to call is the tailnet one, and the socat hop exists only to get a LAN-bound
publish onto an address the guard accepts. Passing the GPU through to the Paperclip VM
is what makes that worth doing: at 2,000 characters per concept the same corpus is
~13 minutes here against ~7 hours on four vCPUs.

## Ground truth: files and directories

Everything is derived from one path — the corpus root:

```
<corpus>/                     # bundles, plus:
  manifest.json               # when the builder wrote it: sources, totals, lint summary, pruned
  embeddings.json             # schema, model, dim, count, complete, text_chars, concept_ids
  embeddings.bin              # flat little-endian float32, count * dim * 4 bytes
  embeddings.ids.jsonl        # journal of a build in flight — the settings page's progress bar
  embeddings.bin.partial      # the matrix being written
<corpus>.requests/
  request.json                # what the plugin asked for
  response.json               # what the runner did, read back by the page
  request.json.done|.failed   # the same request, settled
<corpus>.previous/            # one previous tree, kept across a promote
```

Two properties worth knowing before you operate it:

- **A rebuild is non-disruptive.** The new corpus is built beside the live one and
  renamed into place; the index is only replaced when the build finishes, so agents read
  the old corpus *and* the old index until the swap.
- **A corpus rebuild deletes the index**, because the index lives inside the corpus
  directory. A request that carries an embed block is `mode: "both"` and rebuilds the
  index in the same pass; a plain `okf` request leaves you with a corpus and no vectors.

## Operating it

| Symptom | Cause |
|---|---|
| Every button writes a request that nothing reads | the runner is not running, or `--out` points somewhere else |
| `request declares no sources` | the registry is empty — Settings → Docs → *Sources to build* |
| Search never uses meaning, but the page says it is on | `rag.endpoint` is a blocked address (see the guard) or the index is missing/incomplete |
| `501 does not support embeddings` | the server was started without `--embeddings` |
| `500 input (N tokens) is too large` | the server's `-b`/`-ub` is smaller than the longest document |
| An index build stopped and left a journal | a killed build is resumable: start it again and it continues from the journal |

Health, in one line each:

```bash
systemctl is-active paperclip-docs-runner docs-embed-proxy
docker inspect docs-embed --format '{{.State.Status}}'
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://<tailnet-ip>:8082/v1/embeddings \
  -H 'Content-Type: application/json' -d '{"model":"bge-m3","input":["ping"]}'
```
