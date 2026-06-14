"""Runtime configuration.

All settings are read from the environment with the ``PPV_`` prefix and have
sensible defaults, so the service runs with zero configuration for local use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _default_data_dir() -> Path:
    explicit = os.environ.get("PPV_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "post_prompt_viewer"
    return Path.home() / ".local" / "share" / "post_prompt_viewer"


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    # Storage
    data_dir: Path
    db_path: Path
    recordings_dir: Path
    rejected_dir: Path

    # Server
    host: str
    port: int
    proxy_prefix: str  # e.g. "/viewer" when served behind a reverse proxy

    # Basic auth for the viewer (UI + read APIs). Enabled only when both set.
    auth_user: str
    auth_pass: str

    # Basic auth for the /collect ingest webhook (separate creds; the agent can
    # send them via the user:pass@host URL form). Ingest is open when unset.
    collect_user: str
    collect_pass: str

    # Behaviour
    auto_analyze: bool          # download + analyze recordings on ingest
    enable_summary_memory: bool  # post.cgi fetch_conversation / summary store
    max_list: int               # max rows returned to the index page

    # Recording fetch (optional auth for files.signalwire.com)
    sw_project_id: str
    sw_api_token: str
    recording_layout: str       # "human-left" | "human-right" | "mono"
    allow_local_recordings: bool       # permit file:// / local-path recordings (dev only)
    recording_host_allowlist: tuple    # if non-empty, only fetch recordings from these hosts
    max_ingest_bytes: int              # reject POST bodies larger than this
    playback_format: str               # downsampled playback transcode: "mp3" | "wav"
    keep_original_recordings: bool     # keep the full original after transcode

    # latency_checker analysis params
    energy_threshold: float
    onset_peak_mult: float             # AI onset gate = energy_threshold * this (rejects Opus CNG)
    min_silence_ms: int

    @property
    def has_sw_auth(self) -> bool:
        return bool(self.sw_project_id and self.sw_api_token)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user and self.auth_pass)

    @property
    def collect_auth_enabled(self) -> bool:
        return bool(self.collect_user and self.collect_pass)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = _default_data_dir()
    db_path = Path(os.environ.get("PPV_DB_PATH", str(data_dir / "viewer.sqlite"))).expanduser()
    recordings_dir = Path(
        os.environ.get("PPV_RECORDINGS_DIR", str(data_dir / "recordings"))
    ).expanduser()

    rejected_dir = data_dir / "rejected"
    playback_format = os.environ.get("PPV_PLAYBACK_FORMAT", "mp3").strip().lower()
    if playback_format not in ("mp3", "wav"):
        playback_format = "mp3"

    # Ensure the directories exist up front so storage / cache writes are safe.
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    recordings_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        recordings_dir=recordings_dir,
        rejected_dir=rejected_dir,
        host=os.environ.get("PPV_HOST", "127.0.0.1"),
        port=_env_int("PPV_PORT", 8080),
        proxy_prefix=os.environ.get("PPV_PROXY_PREFIX", "").rstrip("/"),
        auth_user=os.environ.get("PPV_AUTH_USER", ""),
        auth_pass=os.environ.get("PPV_AUTH_PASS", ""),
        collect_user=os.environ.get("PPV_COLLECT_USER", ""),
        collect_pass=os.environ.get("PPV_COLLECT_PASS", ""),
        auto_analyze=_env_bool("PPV_AUTO_ANALYZE", True),
        enable_summary_memory=_env_bool("PPV_ENABLE_SUMMARY_MEMORY", True),
        max_list=_env_int("PPV_MAX_LIST", 500),
        sw_project_id=os.environ.get("PPV_SW_PROJECT_ID", ""),
        sw_api_token=os.environ.get("PPV_SW_API_TOKEN", ""),
        recording_layout=os.environ.get("PPV_RECORDING_LAYOUT", "human-left"),
        allow_local_recordings=_env_bool("PPV_ALLOW_LOCAL_RECORDINGS", False),
        recording_host_allowlist=tuple(
            h.strip().lower()
            for h in os.environ.get("PPV_RECORDING_HOST_ALLOWLIST", "").split(",")
            if h.strip()
        ),
        max_ingest_bytes=_env_int("PPV_MAX_INGEST_BYTES", 32 * 1024 * 1024),
        playback_format=playback_format,
        keep_original_recordings=_env_bool("PPV_KEEP_ORIGINAL_RECORDINGS", False),
        energy_threshold=_env_float("PPV_ENERGY_THRESHOLD", 50.0),
        onset_peak_mult=_env_float("PPV_ONSET_PEAK_MULT", 35.0),
        min_silence_ms=_env_int("PPV_MIN_SILENCE_MS", 2000),
    )
