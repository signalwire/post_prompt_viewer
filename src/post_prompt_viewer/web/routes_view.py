"""Server-rendered pages: the index and the per-call detail view."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import enrich, storage
from ..config import get_settings

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- Jinja filters -------------------------------------------------------- #

def _fmt_dt(us: Optional[int], fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not us:
        return ""
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).strftime(fmt)


def _fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _fmt_ms(v: Optional[float]) -> str:
    return "" if v is None else f"{v:,.0f} ms"


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return ""
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def _fmt_pct(v: Optional[float]) -> str:
    return "" if v is None else f"{v:.0f}%"


def _pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


for _name, _fn in {
    "fmt_dt": _fmt_dt,
    "fmt_dur": _fmt_dur,
    "fmt_ms": _fmt_ms,
    "fmt_num": _fmt_num,
    "fmt_pct": _fmt_pct,
    "pretty": _pretty,
}.items():
    _TEMPLATES.env.filters[_name] = _fn


def _path_url(name, **params):
    """Path-only URL builder that replaces Starlette's absolute ``url_for``.

    Starlette's ``url_for`` returns a fully-qualified URL (scheme + host), which
    becomes ``http://127.0.0.1:<port>/...`` behind a reverse proxy and breaks
    asset/links over HTTPS. Emitting root-relative paths (optionally under
    ``PPV_PROXY_PREFIX``) works at a host root or a sub-path regardless of proxy
    header configuration.
    """
    prefix = get_settings().proxy_prefix
    if name == "static":
        return f"{prefix}/static{params['path']}"
    cid = params.get("call_id", "")
    table = {
        "index": "/",
        "upload": "/upload",
        "detail": f"/c/{cid}",
        "recording_status": f"/api/recording/{cid}",
        "recording_audio": f"/api/recording/{cid}/audio",
        "trigger_analysis": f"/api/recording/{cid}/analyze",
        "call_payload": f"/api/call/{cid}",
    }
    return f"{prefix}{table[name]}"


# Override Starlette's absolute url_for (set via setdefault at env init).
_TEMPLATES.env.globals["url_for"] = _path_url


def _opt_bool(value: Optional[str]) -> Optional[bool]:
    if value in (None, "", "any"):
        return None
    return value in ("1", "true", "yes", "on")


# --- Routes --------------------------------------------------------------- #

@router.get("/", response_class=HTMLResponse, name="index")
async def index(
    request: Request,
    q: Optional[str] = None,
    app: Optional[str] = None,
    rec: Optional[str] = None,
    err: Optional[str] = None,
    sort: str = "received_at",
    dir: str = "desc",
):
    rows = storage.list_calls(
        q=q or None,
        app_name=app or None,
        has_recording=_opt_bool(rec),
        has_errors=_opt_bool(err),
        sort=sort,
        descending=(dir != "asc"),
    )
    filters = {"q": q or "", "app": app or "", "rec": rec or "", "err": err or "",
               "sort": sort, "dir": dir}
    # querystring of active filters, minus sort/dir, so sort links preserve them
    preserved = urlencode({k: v for k, v in
                           (("q", q), ("app", app), ("rec", rec), ("err", err)) if v})
    settings = get_settings()
    # Embed credentials in the copyable post_prompt_url only when the viewer is
    # itself password-protected; otherwise an open viewer would leak the webhook
    # credentials to anyone who can load the page.
    show_creds = settings.collect_auth_enabled and settings.auth_enabled
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "filters": filters,
            "preserved": preserved,
            "apps": storage.distinct_apps(),
            "total": storage.count_calls(),
            "prefix": settings.proxy_prefix,
            "collect_user": settings.collect_user if show_creds else "",
            "collect_pass": settings.collect_pass if show_creds else "",
            "collect_auth_open": settings.collect_auth_enabled and not settings.auth_enabled,
        },
    )


@router.get("/c/{call_id}", response_class=HTMLResponse, name="detail")
async def detail(request: Request, call_id: str, src: str = "blessed"):
    rec = storage.get_call(call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown call")
    payload = rec["payload"]
    recording = storage.get_recording(call_id) or {"status": "absent"}
    audio_present = bool(recording.get("audio_path")) and Path(recording["audio_path"]).exists()
    # Self-heal: if the cached audio was cleared but we still know where to fetch
    # it, re-process on view (when auto-analysis is enabled).
    if (
        recording.get("status") == "done"
        and not audio_present
        and recording.get("source_url")
        and get_settings().auto_analyze
    ):
        try:
            from .. import recordings

            storage.set_recording(call_id, status="pending", error=None)
            recordings.schedule_analysis(call_id)
            recording = storage.get_recording(call_id) or recording
        except Exception:
            pass
    analysis = recording.get("analysis") if recording.get("status") == "done" else None
    alignment = enrich.align_latency(payload, analysis) if analysis else None
    waterfall = enrich.build_waterfall(payload)
    trace = enrich.build_trace(payload)
    events = enrich.build_events(payload)
    # Summary stats over the per-turn mouth-to-ear latency.
    _tl = sorted(r["hero_ms"] for r in trace if r.get("hero_ms"))
    turn_stats = {
        "avg": round(sum(_tl) / len(_tl)),
        "median": round(statistics.median(_tl)),
        "p95": _tl[min(len(_tl) - 1, int(round(0.95 * (len(_tl) - 1))))],
        "max": _tl[-1],
        "count": len(_tl),
    } if _tl else None
    source = "raw" if src == "raw" else "blessed"
    transcript = enrich.build_transcript(payload, source=source)
    # Sorted index of seekable spoken turns, for prev/next + current-turn highlight.
    seek_index = sorted(
        (
            {"idx": t["idx"], "seek": t["seek"], "kind": t["speaker"],
             "label": (t.get("content") or "").strip()[:70]}
            for t in transcript
            if t.get("seek") is not None and t["kind"] in ("assistant", "assistant-manual", "user")
        ),
        key=lambda x: x["seek"],
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "detail.html",
        {
            "rec": rec,
            "payload": payload,
            "src": source,
            "summary": enrich.summary(payload),
            "totals": enrich.totals(payload),
            "call_facts": enrich.call_facts(payload),
            "transcript": transcript,
            "seek_index": seek_index,
            "has_raw_log": bool(payload.get("raw_call_log")),
            "timeline": enrich.build_timeline(payload),
            "latency": enrich.latency_series(payload),
            "functions": enrich.build_functions(payload),
            "global_data": payload.get("global_data"),
            "recording": recording,
            "audio_present": audio_present,
            "analysis": analysis,
            "alignment": alignment,
            "waterfall": waterfall,
            "trace": trace,
            "turn_stats": turn_stats,
            "events": events,
        },
    )
