# Post Prompt Viewer

Inspect SignalWire AI Agent conversations: **transcript, telemetry, and
recording-derived latency** in one place.

When a SignalWire AI Agent finishes a call, it POSTs a rich `post_prompt`
payload describing the whole conversation: the turn-by-turn `call_log`, a typed
`call_timeline` of events, per-turn latency tiers, token/TTS/ASR usage, SWAIG
function calls, and a link to the call recording. Post Prompt Viewer ingests
that payload, stores it, and gives you a clean, drill-down UI to understand what
happened on the call.

It is the modern successor to a pair of Perl CGIs (kept in
[`reference/`](reference/) for guidance): `post.cgi` (the collector) and
`index.cgi` (the viewer).

## Why

The old viewer put everything on one screen and ignored most of the new,
self-describing fields. This rebuild:

- **Separates concerns into tabs** so you can dig into one dimension at a time
  instead of scrolling a wall of data.
- **Cross-validates latency.** The payload reports per-turn `latency`,
  `utterance_latency`, `audio_latency`, and `acoustic_latency`. The last is
  explicitly meant to be compared against a wav-based analyzer. Post Prompt
  Viewer downloads the recording and runs
  [`latency_checker`](https://github.com/signalwire/latency_checker) over it, so
  you can see the server-reported numbers next to the numbers measured from the
  audio.
- **Stays on-brand.** Dark-mode-first, SignalWire design tokens.

## Features

- **Index** — searchable, sortable, filterable list of calls, with an
  **Upload JSON** button and a copyable `post_prompt_url`.
- **Conversation** — clean transcript with per-turn latency/confidence chips,
  expandable model thinking, tool calls, and inline system-log markers. Click any
  turn to hear that moment in the recording.
- **Timeline** — the `call_timeline` event stream, one collapsible line per event.
- **Latency** — a per-turn **pipeline stacked bar** (turn-detection → model TTFT →
  utterance → audio), the user-stopped-to-AI-heard **turn latency** per turn,
  server-reported tiers cross-checked against wav-measured numbers, and the
  annotated waveform with turn markers.
- **Functions** — SWAIG calls with args and collapsible results / `post_response`.
- **Telemetry** — tokens, TTS chars, ASR minutes, and cost factors.
- **Raw** — view / download the stored payload (not embedded inline).
- **Recording player** — play/pause, prev/next turn, current-turn highlight;
  recordings are downsampled to a small mp3 and the original is discarded.

## Install

```bash
# Core viewer (ingest, store, browse)
pip install -e .

# + recording analysis (pulls in latency_checker and its audio stack)
pip install -e ".[recordings]"
# during local development, install the sibling checkout instead:
#   pip install -e ../latency_checker
```

## Run

```bash
post-prompt-viewer            # serves on http://127.0.0.1:8080
# or: ppv --host 0.0.0.0 --port 8080
```

Point your AI Agent's `post_prompt_url` at `http://<host>:8080/collect`.

## Configuration

All optional, read from the environment:

| Variable | Default | Purpose |
|---|---|---|
| `PPV_DATA_DIR` | `~/.local/share/post_prompt_viewer` | Base data directory |
| `PPV_DB_PATH` | `<data>/viewer.sqlite` | SQLite database path |
| `PPV_RECORDINGS_DIR` | `<data>/recordings` | Recording / transcode cache |
| `PPV_HOST` / `PPV_PORT` | `127.0.0.1` / `8080` | Bind address |
| `PPV_PROXY_PREFIX` | (none) | URL prefix behind a reverse proxy |
| `PPV_AUTH_USER` / `PPV_AUTH_PASS` | (none) | Basic-auth the viewer when both set; ingest stays open |
| `PPV_COLLECT_USER` / `PPV_COLLECT_PASS` | (none) | Basic-auth the `/collect` webhook; agent posts to `https://user:pass@host/collect/` |
| `PPV_AUTO_ANALYZE` | `true` | Download + analyze recordings on ingest |
| `PPV_ENABLE_SUMMARY_MEMORY` | `true` | `post.cgi` conversation-memory compatibility |
| `PPV_SW_PROJECT_ID` / `PPV_SW_API_TOKEN` | (none) | Auth for fetching recordings |
| `PPV_RECORDING_LAYOUT` | `human-left` | Stereo channel assignment |
| `PPV_ALLOW_LOCAL_RECORDINGS` | `false` | Permit `file://` / local-path recordings (trusted/dev only) |
| `PPV_RECORDING_HOST_ALLOWLIST` | (none) | Comma-separated hosts allowed for recording fetch |
| `PPV_MAX_INGEST_BYTES` | `33554432` | Reject `/collect` bodies larger than this |
| `PPV_PLAYBACK_FORMAT` | `mp3` | Downsampled 16 kHz playback transcode: `mp3` or `wav` |
| `PPV_KEEP_ORIGINAL_RECORDINGS` | `false` | Keep the full original after transcoding (default: delete it) |
| `PPV_MAX_LIST` | `500` | Max calls shown on the index |
| `PPV_ENERGY_THRESHOLD` | `50` | latency_checker speech-energy threshold |
| `PPV_MIN_SILENCE_MS` | `2000` | latency_checker turn-boundary silence (ms) |

## Architecture

- **Storage** — SQLite (WAL). The full payload is stored verbatim in a JSON
  column; a handful of extracted columns power the index, and FTS5 powers
  transcript search. No schema migration when the payload format grows.
- **Frontend** — server-rendered Jinja2 with small vanilla-JS islands (no build
  step), plus a thin JSON API for async recording-analysis status.
- **Recordings** — fetched (SSRF-guarded) and analyzed in a background thread,
  downsampled to a small mp3 for playback (original discarded, re-fetchable from
  `source_url`), and re-fetched on view if the cache was cleared. Unparseable
  webhook bodies are dead-lettered to `data/rejected/` instead of dropped.
- **Auth** — optional HTTP basic auth: `PPV_AUTH_*` protects the viewer,
  separate `PPV_COLLECT_*` protects the ingest webhook.

See [`docs/ENRICHED_CALL_LOG.md`](docs/ENRICHED_CALL_LOG.md) for the payload
format this consumes.

## Development

```bash
pip install -e ".[dev]"          # tooling; add ,recordings for the audio stack
pytest                            # unit tests run against samples/
black src tests                   # format
```

Run as a background service (PID file + log), like `latency_checker`'s helper:

```bash
./ppv.sh start      # then: status | logs | restart | stop
```

Put deployment-local settings (e.g. `PPV_PORT=9070`) in `./ppv.env`; `ppv.sh` sources it.

### Behind a reverse proxy

To serve under a sub-path, set `PPV_PROXY_PREFIX=/collect`. The app then emits
every URL under that prefix while routing on the **bare** paths a path-stripping
proxy delivers — so do not set a framework `root_path`. Apache:

```apache
RedirectMatch     ^/collect$ /collect/
ProxyPreserveHost On
ProxyPass         /collect/ http://127.0.0.1:9070/
ProxyPassReverse  /collect/ http://127.0.0.1:9070/
```

`ProxyPass /collect/ → :9070/` strips the prefix, so the backend sees `/static`,
`/c/...`, `/` — which is what it routes on. Point the agent's `post_prompt_url`
at `https://host/collect/` (trailing slash); it lands on the backend's `POST /`
ingest alias. To serve at a host root instead, leave `PPV_PROXY_PREFIX` unset and
use `ProxyPass / http://127.0.0.1:9070/`.

Ingest a saved payload by hand:

```bash
curl -X POST http://127.0.0.1:8080/collect \
     -H 'content-type: application/json' \
     --data @samples/conversation_demo.json
```

## License

MIT

