"""Download call recordings and analyze them with ``latency_checker``.

Runs off the request path in a small thread pool. Results (segments,
wav-measured latencies, percentile stats) and a downsampled playback transcode
are cached on disk and recorded in the database. The viewer works without this
module; it just reports that the ``[recordings]`` extra is not installed.
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import socket
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from . import storage
from .config import get_settings
from .enrich import safe_id

log = logging.getLogger("post_prompt_viewer.recordings")

# librosa's mp4/mp3 decode path emits a noisy FutureWarning; silence just that.
warnings.filterwarnings("ignore", message=".*audioread.*", category=FutureWarning)

# Bounded: recording analysis is CPU + memory heavy (librosa decode).
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ppv-analyze")

PLAYBACK_SR = 16_000


def schedule_analysis(call_id: str) -> None:
    """Queue analysis for a call's recording (non-blocking)."""
    _EXECUTOR.submit(_run, call_id)


def _run(call_id: str) -> None:
    try:
        _analyze(call_id)
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        log.exception("recording analysis failed for %s", call_id)
        storage.set_recording(call_id, status="failed", error=str(exc)[:500])


def _ext_from_url(url: str) -> str:
    ext = "." + re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(urlparse(url).path)[1])[:8]
    return ext if len(ext) > 1 else ".audio"


def _host_is_public(host: str) -> bool:
    """True only if every resolved address for ``host`` is a public IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def _assert_fetchable(url: str, settings):
    """Reject SSRF / local-file-read vectors in an attacker-supplied URL."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ("", "file"):
        if not settings.allow_local_recordings:
            raise ValueError("file:// / local recordings are disabled (set PPV_ALLOW_LOCAL_RECORDINGS=1)")
        return parsed
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported recording URL scheme: {scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("recording URL has no host")
    allow = settings.recording_host_allowlist
    if allow and host not in allow:
        raise ValueError(f"recording host not in PPV_RECORDING_HOST_ALLOWLIST: {host}")
    if not _host_is_public(host):
        raise ValueError(f"refusing to fetch recording from a non-public host: {host}")
    return parsed


def _fetch(url: str, dest: Path) -> None:
    """Fetch a recording to ``dest`` after validating the URL (SSRF/LFI guard).

    Remote fetches are http(s) to public hosts only (optionally an explicit host
    allowlist), with redirects disabled. file:// / local paths are refused unless
    PPV_ALLOW_LOCAL_RECORDINGS is set (trusted dev use only).
    """
    settings = get_settings()
    parsed = _assert_fetchable(url, settings)
    scheme = parsed.scheme.lower()
    if scheme in ("", "file"):
        src = Path(unquote(parsed.path) if scheme == "file" else url)
        if not src.exists():
            raise FileNotFoundError(f"local recording not found: {src}")
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        return

    auth = (settings.sw_project_id, settings.sw_api_token) if settings.has_sw_auth else None
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)
    with httpx.stream("GET", url, auth=auth, follow_redirects=False, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_bytes(65536):
                fh.write(chunk)


def _transcode(src: Path, dest: Path, fmt: str = "mp3", sr: int = PLAYBACK_SR) -> None:
    """Downsample the recording to a small playback file via ffmpeg.

    mp3 (~0.4 MB/min) or wav (~3.8 MB/min) at ``sr`` Hz, stereo. ffmpeg is
    already required to decode the source for analysis.
    """
    import subprocess

    codec = ["-c:a", "libmp3lame", "-b:a", "48k"] if fmt == "mp3" else ["-c:a", "pcm_s16le"]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-ar", str(sr), "-ac", "2", *codec, str(dest),
    ]
    subprocess.run(cmd, check=True, timeout=600)


def _analyze(call_id: str) -> None:
    settings = get_settings()
    rec = storage.get_recording(call_id)
    if rec is None:
        return
    url = rec.get("source_url")
    if not url:
        storage.set_recording(call_id, status="absent")
        return

    try:
        from latency_checker import AudioAnalyzer
    except Exception:
        storage.set_recording(
            call_id,
            status="failed",
            error="recording analysis requires the [recordings] extra (latency_checker)",
        )
        return

    rec_dir = settings.recordings_dir
    sid = safe_id(call_id) or "rec"
    original = rec_dir / f"{sid}{_ext_from_url(url)}"

    if not original.exists() or original.stat().st_size == 0:
        storage.set_recording(call_id, status="downloading")
        _fetch(url, original)

    storage.set_recording(call_id, status="analyzing")
    analyzer = AudioAnalyzer(
        file_path=str(original),
        energy_threshold=settings.energy_threshold,
        min_silence_ms=settings.min_silence_ms,
    )
    results = analyzer.analyze()

    # Downsample to a small playback file; fall back to serving the original.
    playback = rec_dir / f"{sid}.{PLAYBACK_SR // 1000}k.{settings.playback_format}"
    try:
        _transcode(original, playback, settings.playback_format)
        audio_path = str(playback)
    except Exception:
        log.warning("transcode failed for %s; serving original file", call_id, exc_info=True)
        audio_path = str(original)

    # Ditch the large original once we have a usable downsampled playback file.
    if audio_path == str(playback) and not settings.keep_original_recordings:
        try:
            original.unlink()
        except OSError:
            pass

    duration = (results.get("file_info") or {}).get("duration")
    storage.set_recording(
        call_id,
        status="done",
        analysis=results,
        audio_path=audio_path,
        duration_s=duration,
        error=None,
    )


def guess_media_type(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"
