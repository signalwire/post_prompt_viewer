# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project overview

Post Prompt Viewer — a FastAPI microservice that ingests SignalWire AI Agent
`post_prompt` payloads, stores them, and presents the conversation, telemetry,
and recording-derived latency in a tabbed drill-down UI. It is the successor to
the Perl `post.cgi` / `index.cgi` kept in `reference/`. The payload format is
documented in `docs/ENRICHED_CALL_LOG.md`.

## Commands

```bash
pip install -e ".[dev]"            # tooling; add ,recordings for the audio stack
pip install -e ../latency_checker  # local sibling checkout (recording analysis)
pytest                             # unit tests, run against samples/
black src tests
post-prompt-viewer --port 8080     # run directly (factory: post_prompt_viewer.app:create_app)
./ppv.sh start                     # background service: start|status|logs|restart|stop
```

`ffmpeg` is required for recording analysis and transcoding. `ppv.sh` sources
`./ppv.env` (gitignored) for deployment-local settings (port, proxy prefix,
credentials). All runtime config is `PPV_*` env vars (see README + `config.py`).

## Architecture (`src/post_prompt_viewer/`)

- `app.py` — FastAPI factory: lifespan (re-queues pending recordings on startup),
  optional basic-auth middleware, mounts routers + `/static`.
- `config.py` — frozen `Settings` from `PPV_*` env; `get_settings()` is
  `lru_cache`d (tests call `get_settings.cache_clear()`).
- `storage.py` — SQLite (WAL), one connection per operation. Tables: `calls`
  (verbatim JSON `payload` column + extracted index columns), `calls_fts` (FTS5
  transcript search), `recordings`, `summaries`.
- `enrich.py` — **pure** payload → view models (no I/O): `derive_index`,
  `build_transcript`, `build_timeline`, `latency_series`, `align_latency`,
  `latency_breakdown`, `build_functions`, `totals`, `summary`, `safe_id`.
- `recordings.py` — background `ThreadPoolExecutor`: download (SSRF-guarded
  `_fetch`) → `latency_checker` `AudioAnalyzer` → ffmpeg transcode to a 16 kHz
  mp3, then delete the original. Status/paths/results cached in `recordings`.
- `web/routes_ingest.py` — `POST /collect` and `/` (webhook drop-in: full-payload
  store, `fetch_conversation` lookup, summary save; bad bodies dead-lettered) and
  `POST /upload`.
- `web/routes_api.py` — JSON: recording status / analyze / audio, raw payload.
- `web/routes_view.py` — server-rendered index + detail; Jinja filters; the
  `_path_url` override (below).
- `web/templates/` (Jinja2) and `web/static/` (vanilla JS: `app.js`, `player.js`,
  `latency.js` — no build step).

## Conventions and gotchas

- **URLs are path-only.** `routes_view._path_url` replaces Starlette's absolute
  `url_for` (which would emit `http://127.0.0.1:PORT/...` and break behind an
  HTTPS proxy). **Any new route used in a template MUST be added to its route
  table**, or rendering 500s with a `KeyError`.
- **No FastAPI `root_path`.** Behind a path-stripping reverse proxy
  (`ProxyPass /collect/ -> :PORT/`) the app routes on the bare paths it receives;
  the prefix (`PPV_PROXY_PREFIX`) is applied only when *emitting* URLs.
- **Auth split:** ingest (`POST /`, `/collect`) is gated by `PPV_COLLECT_*` (open
  if unset); everything else by `PPV_AUTH_*`. `/api/health` is always open.
- **Recordings self-heal:** opening a `done` call whose audio file is missing
  re-fetches + re-analyzes (when `PPV_AUTO_ANALYZE`); analysis itself lives in the
  DB and survives a cache wipe.
- **Security:** `_fetch` blocks `file://` / private-IP / redirect SSRF; `safe_id`
  sanitizes call ids used in filenames; the audio endpoint only serves files
  resolved under the recordings dir; Jinja autoescape is on; SQL is parameterized.
- **Tests** use a per-test tmp data dir (`conftest.py` autouse fixture) and clear
  the settings cache. Keep unit tests free of real network / ffmpeg dependencies.

## Internal

`FINDINGS.md` (gitignored) holds data-quality feedback for the `post_prompt`
generator; it is not part of the published repo.
