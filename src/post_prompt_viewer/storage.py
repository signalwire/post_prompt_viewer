"""SQLite persistence (WAL mode).

The full payload is stored verbatim in a JSON column (``calls.payload``); a
handful of extracted columns power the index page, and an FTS5 table powers
transcript search. Because the payload lives untouched in one column, the
format can grow without a schema migration.

Connections are opened per operation. WAL mode keeps readers (the web app)
from blocking on the background recording-analysis writer.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from . import enrich
from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id              TEXT PRIMARY KEY,
    app_name             TEXT,
    caller_name          TEXT,
    caller_number        TEXT,
    conversation_type    TEXT,
    start_date           INTEGER,
    end_date             INTEGER,
    duration_s           REAL,
    num_turns            INTEGER,
    num_user_turns       INTEGER,
    num_assistant_turns  INTEGER,
    num_functions        INTEGER,
    avg_latency_ms       REAL,
    total_minutes        REAL,
    total_input_tokens   INTEGER,
    total_output_tokens  INTEGER,
    has_recording        INTEGER,
    recording_url        TEXT,
    has_errors           INTEGER,
    has_barge            INTEGER,
    received_at          INTEGER,
    payload              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_start    ON calls(start_date DESC);
CREATE INDEX IF NOT EXISTS idx_calls_received ON calls(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_app      ON calls(app_name);

CREATE VIRTUAL TABLE IF NOT EXISTS calls_fts USING fts5(
    call_id UNINDEXED,
    transcript,
    tokenize = 'porter'
);

CREATE TABLE IF NOT EXISTS recordings (
    call_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- absent|pending|downloading|analyzing|done|failed
    source_url  TEXT,
    audio_path  TEXT,                   -- cached playback transcode
    analysis    TEXT,                   -- latency_checker results (JSON)
    error       TEXT,
    duration_s  REAL,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS summaries (
    conversation_id TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    summary         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_conv ON summaries(conversation_id);
"""

# Index columns selected for list/detail (everything except the big payload blob).
_INDEX_COLS = (
    "call_id, app_name, caller_name, caller_number, conversation_type, "
    "start_date, end_date, duration_s, num_turns, num_user_turns, "
    "num_assistant_turns, num_functions, avg_latency_ms, total_minutes, "
    "total_input_tokens, total_output_tokens, has_recording, recording_url, "
    "has_errors, has_barge, received_at"
)

_SORTABLE = {
    "received_at": "received_at",
    "start_date": "start_date",
    "duration": "duration_s",
    "turns": "num_turns",
    "latency": "avg_latency_ms",
}


def now_us() -> int:
    return int(time.time() * 1_000_000)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_settings().db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #

def save_call(payload: dict, received_at_us: Optional[int] = None) -> str:
    """Store (or replace) a payload and refresh its index + FTS rows."""
    received_at_us = received_at_us or now_us()
    idx = enrich.derive_index(payload, received_at_us)
    call_id = idx["call_id"]
    if not call_id:
        raise ValueError("payload has no call_id / ai_session_id")

    cols = list(idx.keys()) + ["payload"]
    placeholders = ", ".join(["?"] * len(cols))
    values = [idx[k] for k in idx] + [json.dumps(payload, ensure_ascii=False)]

    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO calls ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        conn.execute("DELETE FROM calls_fts WHERE call_id = ?", (call_id,))
        conn.execute(
            "INSERT INTO calls_fts (call_id, transcript) VALUES (?, ?)",
            (call_id, enrich.transcript_text(payload)),
        )
        # Seed a recordings row without clobbering an existing analysis.
        status = "pending" if idx["has_recording"] else "absent"
        conn.execute(
            "INSERT OR IGNORE INTO recordings (call_id, status, source_url, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (call_id, status, idx["recording_url"], received_at_us),
        )
    return call_id


def delete_call(call_id: str) -> Optional[dict]:
    """Delete a call and its FTS + recording rows. Returns the (now-removed)
    recording row so the caller can clean up cached audio, an empty dict when
    there was no recording row, or None if the call_id is unknown."""
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM calls WHERE call_id = ?", (call_id,)).fetchone():
            return None
        rec = conn.execute("SELECT * FROM recordings WHERE call_id = ?", (call_id,)).fetchone()
        conn.execute("DELETE FROM calls WHERE call_id = ?", (call_id,))
        conn.execute("DELETE FROM calls_fts WHERE call_id = ?", (call_id,))
        conn.execute("DELETE FROM recordings WHERE call_id = ?", (call_id,))
    return dict(rec) if rec else {}


def get_call(call_id: str) -> Optional[dict]:
    """Return the index columns plus the parsed payload, or None."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_INDEX_COLS}, payload FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
    if row is None:
        return None
    rec = dict(row)
    rec["payload"] = json.loads(rec["payload"])
    return rec


def _fts_query(q: str) -> str:
    """Quote each term so user input cannot break FTS5 syntax (implicit AND)."""
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def list_calls(
    q: Optional[str] = None,
    app_name: Optional[str] = None,
    has_recording: Optional[bool] = None,
    has_errors: Optional[bool] = None,
    sort: str = "received_at",
    descending: bool = True,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list:
    """Filtered, sorted index rows (no payload blob)."""
    settings = get_settings()
    limit = settings.max_list if limit is None else limit
    where: list = []
    params: list = []

    if q:
        with _connect() as conn:
            ids = [
                r["call_id"]
                for r in conn.execute(
                    "SELECT call_id FROM calls_fts WHERE calls_fts MATCH ?", (_fts_query(q),)
                )
            ]
        if not ids:
            return []
        where.append(f"call_id IN ({', '.join(['?'] * len(ids))})")
        params.extend(ids)
    if app_name:
        where.append("app_name = ?")
        params.append(app_name)
    if has_recording is not None:
        where.append("has_recording = ?")
        params.append(1 if has_recording else 0)
    if has_errors is not None:
        where.append("has_errors = ?")
        params.append(1 if has_errors else 0)

    order_col = _SORTABLE.get(sort, "received_at")
    direction = "DESC" if descending else "ASC"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {_INDEX_COLS} FROM calls {clause} "
        f"ORDER BY {order_col} {direction} NULLS LAST LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def distinct_apps() -> list:
    with _connect() as conn:
        return [
            r["app_name"]
            for r in conn.execute(
                "SELECT DISTINCT app_name FROM calls WHERE app_name != '' ORDER BY app_name"
            )
        ]


def count_calls() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]


# --------------------------------------------------------------------------- #
# Recordings
# --------------------------------------------------------------------------- #

def get_recording(call_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM recordings WHERE call_id = ?", (call_id,)).fetchone()
    if row is None:
        return None
    rec = dict(row)
    if rec.get("analysis"):
        rec["analysis"] = json.loads(rec["analysis"])
    return rec


def set_recording(call_id: str, **fields: Any) -> None:
    """Upsert recording fields (status/audio_path/analysis/error/duration_s)."""
    if "analysis" in fields and fields["analysis"] is not None and not isinstance(
        fields["analysis"], str
    ):
        fields["analysis"] = json.dumps(fields["analysis"], ensure_ascii=False)
    fields["updated_at"] = now_us()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT call_id FROM recordings WHERE call_id = ?", (call_id,)
        ).fetchone()
        if existing is None:
            cols = ["call_id"] + list(fields.keys())
            conn.execute(
                f"INSERT INTO recordings ({', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))})",
                [call_id] + list(fields.values()),
            )
        else:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE recordings SET {sets} WHERE call_id = ?",
                list(fields.values()) + [call_id],
            )


def pending_recordings() -> list:
    """call_ids whose recordings still need analysis (for startup catch-up)."""
    with _connect() as conn:
        return [
            r["call_id"]
            for r in conn.execute(
                "SELECT call_id FROM recordings WHERE status IN ('pending', 'downloading', 'analyzing')"
            )
        ]


# --------------------------------------------------------------------------- #
# Conversation memory (post.cgi fetch_conversation / summary compatibility)
# --------------------------------------------------------------------------- #

def append_summary(conversation_id: str, summary_text: str, ts_us: Optional[int] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO summaries (conversation_id, ts, summary) VALUES (?, ?, ?)",
            (conversation_id, ts_us or now_us(), summary_text.strip()),
        )


def get_summaries(conversation_id: str) -> list:
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT ts, summary FROM summaries WHERE conversation_id = ? ORDER BY ts",
                (conversation_id,),
            )
        ]
