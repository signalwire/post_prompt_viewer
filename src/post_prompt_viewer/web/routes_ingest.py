"""Ingest endpoint — the modern ``post.cgi``.

A single POST URL handles three payload shapes the live AI agent sends, just
like the original CGI:

1. ``action: "fetch_conversation"`` + ``conversation_id`` -> return any stored
   summary for that conversation (used mid-call to recall prior context).
2. ``conversation_id`` + ``conversation_summary`` -> append a dated summary line
   to that conversation's memory.
3. A full ``post_prompt`` payload (has ``call_log`` / ``call_id``) -> store it
   and, if configured, kick off background recording analysis.

The response shapes match the original CGI so this is a drop-in replacement.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import storage
from ..config import get_settings

log = logging.getLogger("post_prompt_viewer.ingest")

try:  # America/Chicago to match the original CGI's "CT" stamp
    from zoneinfo import ZoneInfo

    _CHICAGO = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _CHICAGO = None

router = APIRouter()


async def _parse_body(request: Request):
    """Decode the request body as a JSON object, tolerating CGI-style form
    POSTs. Returns the dict, or None if nothing parseable was found."""
    raw = await request.body()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    try:
        form = await request.form()
        if "POSTDATA" in form:
            obj = json.loads(form["POSTDATA"])
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return None


def _summary_line(text: str) -> str:
    now = datetime.now(_CHICAGO) if _CHICAGO else datetime.now(timezone.utc)
    stamp = now.strftime("%m/%d/%y %I:%M%p")
    return f"- Call {stamp} CT: {text.strip()}"


def _schedule_analysis(call_id: str) -> None:
    try:
        from .. import recordings
    except Exception:
        return
    recordings.schedule_analysis(call_id)


def _ingest_payload(data: dict, settings):
    """Store a full post_prompt payload and queue recording analysis. Returns
    the call id, or None if the data isn't a storable payload."""
    if not (data.get("call_log") or data.get("call_id")):
        return None
    try:
        call_id = storage.save_call(data)
    except ValueError:
        return None
    if call_id and settings.auto_analyze:
        rec = storage.get_recording(call_id)
        if rec and rec.get("status") == "pending":
            _schedule_analysis(call_id)
    return call_id


def _dead_letter(raw, reason: str, settings) -> None:
    """Preserve an unprocessable webhook body instead of dropping it silently."""
    try:
        path = settings.rejected_dir / f"{int(time.time() * 1_000_000)}.{reason}.json"
        path.write_bytes(bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8"))
        log.warning("rejected %s payload (%d bytes) saved to %s", reason, len(raw), path)
    except Exception:
        log.exception("failed to write rejected payload")


@router.post("/collect")
@router.post("/")  # also answer at the proxied prefix root (e.g. POST /collect/ -> backend /)
async def collect(request: Request):
    settings = get_settings()
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_ingest_bytes:
        return JSONResponse({"response": "payload too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > settings.max_ingest_bytes:
        return JSONResponse({"response": "payload too large"}, status_code=413)
    data = await _parse_body(request)

    # Unparseable body: preserve it rather than dropping it silently.
    if data is None:
        if raw.strip():
            _dead_letter(raw, "unparseable", settings)
        return JSONResponse({"response": "data received"})

    handled = False

    # 1. Conversation-memory lookup.
    if (
        settings.enable_summary_memory
        and data.get("action") == "fetch_conversation"
        and data.get("conversation_id")
    ):
        lines = [s["summary"] for s in storage.get_summaries(data["conversation_id"])]
        if lines:
            return JSONResponse(
                {"response": "Conversation found", "conversation_summary": "\n".join(lines) + "\n"}
            )
        return JSONResponse({"response": "No previous conversation found.\n"})

    # 2. Conversation-memory save (may co-occur with a full payload).
    if (
        settings.enable_summary_memory
        and data.get("conversation_id")
        and data.get("conversation_summary")
    ):
        storage.append_summary(data["conversation_id"], _summary_line(data["conversation_summary"]))
        handled = True

    # 3. Full post_prompt payload.
    if _ingest_payload(data, settings):
        handled = True

    # Anything we didn't recognize is preserved, not dropped on the floor.
    if not handled and raw.strip():
        _dead_letter(raw, "unrecognized", settings)

    return JSONResponse({"response": "data received"})


@router.post("/upload")
async def upload(request: Request):
    """Manual upload of a saved post_prompt JSON from the viewer UI. Same ingest
    path as /collect, but gated by the viewer login (not the webhook creds)."""
    settings = get_settings()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("file")
        raw = await f.read() if f is not None else b""
    else:
        raw = await request.body()
    if len(raw) > settings.max_ingest_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="not valid JSON")
    if not (isinstance(data, dict) and (data.get("call_log") or data.get("call_id"))):
        raise HTTPException(status_code=400, detail="not a post_prompt payload (needs call_log or call_id)")
    call_id = _ingest_payload(data, settings)
    if not call_id:
        raise HTTPException(status_code=400, detail="could not store payload")
    return {"call_id": call_id, "url": f"{settings.proxy_prefix}/c/{call_id}"}
