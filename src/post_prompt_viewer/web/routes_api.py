"""Thin JSON API.

Mostly serves the async recording-analysis status that the Latency tab polls,
plus the cached playback audio. Page rendering lives in ``routes_view``.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from .. import storage
from ..config import get_settings
from ..enrich import safe_id

router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    return {"status": "ok", "calls": storage.count_calls()}


@router.get("/call/{call_id}")
async def call_payload(call_id: str):
    """The raw stored payload (used by the Raw tab's download link)."""
    rec = storage.get_call(call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown call")
    return rec["payload"]


@router.get("/recording/{call_id}")
async def recording_status(call_id: str):
    rec = storage.get_recording(call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown call")
    return {
        "call_id": call_id,
        "status": rec["status"],
        "error": rec.get("error"),
        "duration_s": rec.get("duration_s"),
        "has_audio": bool(rec.get("audio_path")),
        "analysis": rec.get("analysis"),
    }


@router.post("/recording/{call_id}/analyze")
async def trigger_analysis(call_id: str):
    """Manually (re)queue analysis for a call's recording."""
    rec = storage.get_recording(call_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="unknown call")
    if not rec.get("source_url"):
        raise HTTPException(status_code=409, detail="call has no recording")
    try:
        from .. import recordings
    except Exception as exc:  # extra not installed
        raise HTTPException(
            status_code=501,
            detail="recording analysis unavailable; install the [recordings] extra",
        ) from exc
    storage.set_recording(call_id, status="pending", error=None)
    recordings.schedule_analysis(call_id)
    return {"call_id": call_id, "status": "pending"}


@router.post("/calls/delete")
async def delete_calls(ids: list[str] = Body(..., embed=True)):
    """Delete calls (rows + FTS + recording row) and their cached audio files.

    POST so nothing deletes by accident; gated by ``PPV_AUTH`` like the rest of
    the API. File removal is restricted to the recordings cache dir.
    """
    rec_dir = get_settings().recordings_dir.resolve()
    deleted = 0
    for cid in ids:
        rec = storage.delete_call(cid)
        if rec is None:  # unknown call_id — skip
            continue
        deleted += 1
        candidates = []
        if rec.get("audio_path"):
            candidates.append(Path(rec["audio_path"]))
        sid = safe_id(cid)
        if sid:
            candidates.extend(rec_dir.glob(sid + ".*"))
        for p in candidates:
            try:
                rp = p.resolve()
                if rp.is_relative_to(rec_dir) and rp.is_file():
                    rp.unlink()
            except OSError:
                pass
    return {"deleted": deleted}


@router.get("/recording/{call_id}/audio")
async def recording_audio(call_id: str):
    rec = storage.get_recording(call_id)
    if rec is None or not rec.get("audio_path"):
        raise HTTPException(status_code=404, detail="no cached audio")
    path = Path(rec["audio_path"]).resolve()
    rec_dir = get_settings().recordings_dir.resolve()
    # Defense in depth: never serve a file outside the recordings cache.
    if not path.is_relative_to(rec_dir) or not path.exists():
        raise HTTPException(status_code=404, detail="cached audio missing")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(str(path), media_type=media_type or "application/octet-stream")
