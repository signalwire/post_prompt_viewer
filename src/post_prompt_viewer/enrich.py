"""Turn a raw ``post_prompt`` payload into clean, display-ready view models.

This module is pure: it takes a parsed payload (a ``dict``) and returns plain
dicts/lists. No I/O, no database, no network. That keeps it trivial to test
against ``samples/`` and lets the web layer and the JSON API share one source
of truth.

The payload format is documented in ``docs/ENRICHED_CALL_LOG.md``.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

# SignalWire AI runtime price, used only for an illustrative cost estimate.
AI_RUNTIME_USD_PER_MIN = 0.16

# call_log roles that carry spoken/typed conversational content.
_CONTENT_ROLES = {"user", "assistant", "assistant-manual"}

# Human labels for call_timeline / system-log event types.
EVENT_LABELS = {
    "session_start": "Session started",
    "session_end": "Session ended",
    "step_change": "Step change",
    "context_enter": "Context switch",
    "reset": "Conversation reset",
    "gather_start": "Gather started",
    "gather_question": "Gather question",
    "gather_answer": "Gather answer",
    "gather_reject": "Gather rejected",
    "gather_complete": "Gather complete",
    "function_call": "Function call",
    "function_error": "Function error",
    "startup_hook": "Startup hook",
    "hangup_hook": "Hangup hook",
    "summarize_start": "Summarize started",
    "check_for_input": "Input poll",
    "manual_say": "Manual say",
    "attention_timeout": "Attention timeout",
    "filler": "Filler audio",
    "hearing_hint": "Hearing hint rewrite",
    "pronounce_rule": "Pronounce rewrite",
    "pronounce": "Pronounce rewrite",
    "auto_correct": "Auto-correct",
    "text_normalize": "Text normalize",
    "user_input": "User input",
    "ai_response": "AI response",
    "tool_result": "Tool result",
}

LATENCY_TIERS = ("latency", "utterance_latency", "audio_latency", "acoustic_latency")


# --------------------------------------------------------------------------- #
# Time helpers (payload timestamps are microseconds since the epoch, UTC)
# --------------------------------------------------------------------------- #

def us_to_dt(us: Optional[int]) -> Optional[datetime]:
    if not us:
        return None
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)


def fmt_ts(us: Optional[int], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = us_to_dt(us)
    if dt is None:
        return ""
    # include centiseconds for sub-second ordering, like the old viewer
    return dt.strftime(fmt) + f".{(us % 1_000_000) // 10_000:02d}"


def us_to_s(us: Optional[int]) -> Optional[float]:
    return None if not us else us / 1_000_000


def fmt_elapsed(old_us: Optional[int], new_us: Optional[int]) -> str:
    if not old_us or not new_us:
        return ""
    diff = (new_us - old_us) / 1_000_000
    if diff < 0:
        return ""
    minutes, seconds = divmod(diff, 60)
    return f"{int(minutes)}m {seconds:.1f}s"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}m {secs:.1f}s"


def _record_start(payload: dict):
    """record_call_start as a float (micros), tolerating the stringified form."""
    rs = (payload.get("SWMLVars") or {}).get("record_call_start")
    try:
        return float(rs) if rs else None
    except (TypeError, ValueError):
        return None


def _seek(entry: dict, record_start) -> Optional[float]:
    """Recording-relative seconds for a call_log entry (for audio seeking)."""
    if not record_start:
        return None
    ts = entry.get("start_timestamp") or entry.get("timestamp")
    try:
        return round(max(0.0, (float(ts) - record_start) / 1_000_000), 2) if ts else None
    except (TypeError, ValueError):
        return None


def _loads(maybe_json: Any) -> Any:
    """Best-effort JSON decode; returns the input unchanged if it is not JSON."""
    if not isinstance(maybe_json, str):
        return maybe_json
    try:
        return json.loads(maybe_json)
    except (ValueError, TypeError):
        return maybe_json


_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.\-]")


def safe_id(value: str) -> str:
    """Filesystem- and URL-safe call id: keep only [A-Za-z0-9_.-], strip leading
    dots, cap length. Prevents path traversal via attacker-controlled call ids."""
    return _UNSAFE_ID.sub("", value or "").lstrip(".")[:128]


# --------------------------------------------------------------------------- #
# Index record (the columns the storage layer extracts and indexes)
# --------------------------------------------------------------------------- #

def _caller_name(payload: dict) -> str:
    return (
        payload.get("caller_id_name")
        or payload.get("global_data", {}).get("caller_id_name")
        or ""
    )


def _caller_number(payload: dict) -> str:
    return (
        payload.get("caller_id_number")
        or payload.get("caller_id_num")  # old field name
        or payload.get("global_data", {}).get("caller_id_number")
        or ""
    )


def _has_errors(call_log: list) -> bool:
    for e in call_log:
        action = e.get("action")
        md = e.get("metadata") or {}
        if action in {"function_error"}:
            return True
        if action == "manual_say" and (md.get("is_error") or e.get("is_error")):
            return True
        if action == "session_end":
            reason = md.get("reason") or e.get("reason")
            if reason and reason not in {"normal", "hangup"}:
                return True
        if e.get("fatal_error") or md.get("fatal_error"):
            return True
    return False


def _call_window(payload: dict):
    """Best (start, end) micros for the call. Falls back to the AI-session
    window when ``call_end_date`` is missing or zero (some payloads omit it)."""
    s, e = payload.get("call_start_date"), payload.get("call_end_date")
    if s and e and e > s:
        return s, e
    ai_s, ai_e = payload.get("ai_start_date"), payload.get("ai_end_date")
    if ai_s and ai_e and ai_e > ai_s:
        return ai_s, ai_e
    return None, None


def derive_index(payload: dict, received_at_us: int) -> dict:
    """Extract the flat, indexable summary stored alongside the raw payload."""
    call_log = payload.get("call_log") or []
    start = payload.get("call_start_date")
    end = payload.get("call_end_date")
    ws, we = _call_window(payload)
    duration_s = (we - ws) / 1_000_000 if ws is not None else None

    user_turns = [e for e in call_log if e.get("role") == "user" and e.get("content")]
    asst_turns = [
        e for e in call_log if e.get("role") in {"assistant", "assistant-manual"} and e.get("content")
    ]
    audio_lat = [e["audio_latency"] for e in asst_turns
                 if isinstance(e.get("audio_latency"), (int, float)) and e["audio_latency"] > 0]

    swaig = payload.get("swaig_log") or []
    tool_entries = [e for e in call_log if e.get("role") == "tool"]

    return {
        "call_id": safe_id(payload.get("call_id") or payload.get("ai_session_id") or ""),
        "app_name": payload.get("app_name") or "",
        "caller_name": _caller_name(payload),
        "caller_number": _caller_number(payload),
        "conversation_type": payload.get("conversation_type") or "",
        "start_date": start,
        "end_date": end,
        "duration_s": duration_s,
        "num_turns": len(user_turns) + len(asst_turns),
        "num_user_turns": len(user_turns),
        "num_assistant_turns": len(asst_turns),
        "num_functions": len(swaig) or len(tool_entries),
        "avg_latency_ms": (sum(audio_lat) / len(audio_lat)) if audio_lat else None,
        "total_minutes": payload.get("total_minutes"),
        "total_input_tokens": payload.get("total_input_tokens"),
        "total_output_tokens": payload.get("total_output_tokens"),
        "has_recording": bool(payload.get("SWMLVars", {}).get("record_call_url")),
        "recording_url": payload.get("SWMLVars", {}).get("record_call_url") or "",
        "has_errors": _has_errors(call_log),
        "has_barge": any(e.get("barged") for e in call_log),
        "received_at": received_at_us,
    }


# --------------------------------------------------------------------------- #
# Transcript (the Conversation tab)
# --------------------------------------------------------------------------- #

def _latency_tiers(entry: dict) -> dict:
    # Treat 0 (and negatives) as "not measured": some payloads emit a tier on
    # every turn but leave the value at 0 when it was never actually computed.
    return {tier: entry[tier] for tier in LATENCY_TIERS
            if isinstance(entry.get(tier), (int, float)) and entry[tier] > 0}


def _tool_call_summary(entry: dict) -> list:
    calls = []
    for tc in entry.get("tool_calls") or []:
        fn = (tc.get("function") or {})
        calls.append({
            "name": fn.get("name", "?"),
            "arguments": _loads(fn.get("arguments")),
            "id": tc.get("id"),
        })
    return calls


def build_transcript(payload: dict, source: str = "blessed") -> list:
    """Normalize ``call_log`` (blessed) or ``raw_call_log`` (raw) into turns.

    Each returned turn is a dict the template can render directly. ``kind``
    drives styling and ``speaker`` groups turns into human/ai/system/tool lanes.
    """
    log_key = "raw_call_log" if source == "raw" else "call_log"
    entries = payload.get(log_key) or payload.get("call_log") or []
    record_start = _record_start(payload)

    turns = []
    last_ts = None
    # Attach per-response performance metrics (times[]) to spoken AI turns by
    # response *text*. Order is not reliable: tool-call generations also produce
    # times[] entries, so the Nth spoken turn is not the Nth times[] entry.
    _times_pool = [[(t.get("response") or "").strip(), t, False] for t in (payload.get("times") or [])]

    def _match_perf(content: Optional[str]) -> Optional[dict]:
        norm = (content or "").strip()
        if not norm:
            return None
        for item in _times_pool:
            text, t, consumed = item
            if consumed or not text:
                continue
            if text == norm or norm.startswith(text) or text.startswith(norm):
                item[2] = True
                return {
                    "answer_time": t.get("answer_time"),
                    "tps": t.get("tps") or t.get("avg_tps"),
                    "tokens": t.get("tokens"),
                    "words": t.get("response_word_count"),
                }
        return None

    for idx, e in enumerate(entries):
        role = e.get("role")
        ts = e.get("timestamp")
        content = e.get("content")

        turn = {
            "idx": idx,
            "seek": _seek(e, record_start),
            "role": role,
            "kind": role,
            "speaker": "system",
            "content": content,
            "timestamp": ts,
            "ts_str": fmt_ts(ts) if ts else "",
            "elapsed": fmt_elapsed(last_ts, ts) if (ts and last_ts) else "",
            "collapsible": False,
            "meta": {},
        }

        if role in {"assistant", "assistant-manual"} and content:
            turn["speaker"] = "ai"
            turn["latency"] = _latency_tiers(e)
            if e.get("barged"):
                turn["barge"] = {
                    "elapsed_ms": e.get("barge_elapsed_ms"),
                    "heard": e.get("text_heard_approx"),
                    "total": e.get("text_spoken_total"),
                }
            perf = _match_perf(content)
            if perf:
                turn["perf"] = perf

        elif role == "assistant" and e.get("tool_calls"):
            turn["kind"] = "tool-call"
            turn["speaker"] = "ai"
            turn["tool_calls"] = _tool_call_summary(e)

        elif role == "assistant-thinking":
            turn["speaker"] = "ai"
            turn["collapsible"] = True

        elif role == "user":
            turn["speaker"] = "human"
            if isinstance(e.get("confidence"), (int, float)):
                turn["confidence"] = e["confidence"] * 100
            # Recognized entity (email / phone / ssn / …): normalized + validated.
            ent = e.get("entity")
            if isinstance(ent, dict) and ent.get("value"):
                turn["entity"] = {
                    "type": ent.get("type"),
                    "value": ent.get("value"),
                    "valid": bool(ent.get("valid")),
                }
            # End-of-turn decision: how the boundary was chosen, and how sure.
            eot = e.get("eot")
            if isinstance(eot, dict) and eot.get("basis"):
                conf = eot.get("confidence")
                turn["eot"] = {
                    "basis": eot["basis"],
                    "confidence": conf * 100 if isinstance(conf, (int, float)) else None,
                }
            # ASR / turn-detection timing (ms): how long finalizing this turn took.
            asr = {}
            timing = e.get("timing")
            if isinstance(timing, dict):
                for k in ("commit_latency_ms", "hold_ms", "segments"):
                    if isinstance(timing.get(k), (int, float)):
                        asr[k] = timing[k]
            for k in ("speaking_to_final_event", "speaking_to_turn_detection",
                      "turn_detection_to_final_event"):
                if isinstance(e.get(k), (int, float)):
                    asr[k] = e[k]
            if asr:
                turn["asr"] = asr
            for k in ("speaker", "barge_count", "merge_count", "merged"):
                if e.get(k) not in (None, 0, False):
                    turn["meta"][k] = e[k]

        elif role == "tool":
            turn["speaker"] = "tool"
            turn["function_name"] = e.get("function_name")
            turn["latency"] = {
                k: e[k]
                for k in ("latency", "function_latency", "execution_latency")
                if isinstance(e.get(k), (int, float))
            }

        elif role == "system-log":
            turn["kind"] = "event"
            turn["action"] = e.get("action")
            turn["label"] = EVENT_LABELS.get(e.get("action"), e.get("action") or "event")
            turn["metadata"] = e.get("metadata") or {}

        elif role == "system":
            # The first/large system entry is the prompt; make it collapsible.
            turn["collapsible"] = bool(content and len(content) > 200)

        turns.append(turn)
        if ts:
            last_ts = ts

    return turns


# --------------------------------------------------------------------------- #
# Timeline (the Timeline tab)
# --------------------------------------------------------------------------- #

def build_timeline(payload: dict) -> list:
    """Flat, labeled event stream with elapsed deltas.

    Prefers the payload's ``call_timeline`` (already flattened); otherwise
    derives a minimal stream from ``system-log`` entries.
    """
    raw = payload.get("call_timeline")
    events = []
    if raw:
        for ev in raw:
            ev = dict(ev)
            ts = ev.pop("ts", None)
            etype = ev.pop("type", "event")
            events.append((ts, etype, ev))
    else:
        for e in payload.get("call_log") or []:
            if e.get("role") == "system-log":
                events.append((e.get("timestamp"), e.get("action") or "event", e.get("metadata") or {}))

    out = []
    last_ts = None
    for ts, etype, details in events:
        out.append({
            "ts": ts,
            "ts_str": fmt_ts(ts) if ts else "",
            "elapsed": fmt_elapsed(last_ts, ts) if (ts and last_ts) else "",
            "type": etype,
            "label": EVENT_LABELS.get(etype, etype),
            "details": {k: v for k, v in details.items() if v not in (None, "")},
        })
        if ts:
            last_ts = ts
    return out


# --------------------------------------------------------------------------- #
# Latency series (the Latency tab — server-reported side)
# --------------------------------------------------------------------------- #

def latency_series(payload: dict) -> dict:
    """Per-AI-turn latency tiers, plus summary stats over audio_latency."""
    points = []
    for e in payload.get("call_log") or []:
        if e.get("role") in {"assistant", "assistant-manual"} and _latency_tiers(e):
            tiers = _latency_tiers(e)
            points.append({
                "ts": e.get("timestamp"),
                "start_ts": e.get("start_timestamp"),
                "text": (e.get("content") or "").strip()[:80],
                **tiers,
            })

    def _stats(key: str) -> Optional[dict]:
        vals = sorted(p[key] for p in points if key in p)
        if not vals:
            return None
        return {
            "avg": sum(vals) / len(vals),
            "median": statistics.median(vals),
            "min": vals[0],
            "max": vals[-1],
            "p95": vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))],
            "count": len(vals),
        }

    return {
        "points": points,
        "stats": {tier: _stats(tier) for tier in LATENCY_TIERS},
        "has_acoustic": any("acoustic_latency" in p for p in points),
    }


def align_latency(payload: dict, analysis: dict) -> dict:
    """Cross-check server-reported latency against recording-measured latency.

    latency_checker reports Human->AI response latencies in *seconds*, measured
    from the recording audio. We convert to milliseconds and, when the recording
    start time is known, match each wav-measured latency to the nearest AI turn
    (by absolute time) so it can sit beside that turn's server-reported
    audio/acoustic latency. ``acoustic_latency`` is the field the payload spec
    says to compare against a wav analyzer, so it is the preferred reference.
    """
    server = latency_series(payload)
    spoints = server["points"]
    wav = analysis.get("latencies") or []
    record_start = (payload.get("SWMLVars") or {}).get("record_call_start")
    try:  # some payloads stringify this timestamp; others send an int
        record_start = float(record_start) if record_start is not None else None
    except (TypeError, ValueError):
        record_start = None

    pairs = []
    used = set()
    if record_start:
        for w in wav:
            if w.get("ai_start") is None:
                continue
            abs_ai = record_start + float(w["ai_start"]) * 1_000_000
            best_i, best_d = None, None
            for i, p in enumerate(spoints):
                if i in used:
                    continue
                st = p.get("start_ts") or p.get("ts")
                try:
                    st = float(st)
                except (TypeError, ValueError):
                    continue
                d = abs(st - abs_ai)
                if best_d is None or d < best_d:
                    best_i, best_d = i, d
            if best_i is not None and best_d is not None and best_d <= 3_000_000:
                used.add(best_i)
                p = spoints[best_i]
                wav_ms = round(w["latency"] * 1000)
                ref = p.get("acoustic_latency")
                if ref is None:
                    ref = p.get("audio_latency")
                pairs.append({
                    "wav_ms": wav_ms,
                    "audio_latency": p.get("audio_latency"),
                    "acoustic_latency": p.get("acoustic_latency"),
                    "text": p.get("text"),
                    "delta_ms": (wav_ms - ref) if ref is not None else None,
                })

    stats = analysis.get("statistics") or {}

    def _ms(v):
        return round(v * 1000) if isinstance(v, (int, float)) else None

    def _rnd(d, key):
        v = (d or {}).get(key)
        return round(v) if isinstance(v, (int, float)) else None

    audio = server["stats"].get("audio_latency") or {}
    acoustic = server["stats"].get("acoustic_latency") or {}
    aggregate = {
        "wav_avg": _ms(stats.get("avg_latency")),
        "wav_median": _ms(stats.get("median_latency")),
        "wav_p95": _ms(stats.get("p95_latency")),
        "wav_count": stats.get("num_latencies"),
        "server_audio_avg": _rnd(audio, "avg"),
        "server_audio_median": _rnd(audio, "median"),
        "server_audio_p95": _rnd(audio, "p95"),
        "server_acoustic_avg": _rnd(acoustic, "avg"),
    }
    return {
        "pairs": pairs,
        "matched": len(pairs),
        "wav_count": len(wav),
        "server_count": len(spoints),
        "has_record_start": bool(record_start),
        "aggregate": aggregate,
    }


def _pos(value):
    """Numeric value if it is a positive number, else None (0 = not measured)."""
    return value if isinstance(value, (int, float)) and value > 0 else None


def latency_breakdown(payload: dict) -> list:
    """Per-AI-turn pipeline decomposition for a stacked bar, anchored at
    "user stopped talking" per ENRICHED_CALL_LOG.md.

    Segments (ms), left to right:
        turn detection (eos_to_push) | ASR poll | model TTFT | model->utterance |
        utterance->audio

    The three model segments always sum to ``audio_latency`` (they share the
    ``request_detect_time`` anchor). The pre-model portion is ``total - audio``:
    when ``eos_to_push`` is known (field or derived from the raw ``*_wall_us``
    timestamps) it is split into turn-detection + poll; otherwise it is shown as
    a single unattributed "turn detection" segment. ``total`` is
    ``acoustic_latency`` when measured, else ``eos_to_push + audio_latency``.
    Segments always sum to ``total``. Turns with no audio timing (filler /
    manual_say / tool turns) are skipped.
    """
    rows = []
    record_start = _record_start(payload)
    for idx, e in enumerate(payload.get("call_log") or []):
        if e.get("role") not in ("assistant", "assistant-manual"):
            continue
        aud = _pos(e.get("audio_latency"))
        if not aud:
            continue
        lat, utt = _pos(e.get("latency")), _pos(e.get("utterance_latency"))
        ac = _pos(e.get("acoustic_latency"))

        eos = _pos(e.get("eos_to_push_latency"))
        derived_eos = False
        if eos is None:
            lwe, sp = e.get("last_word_end_wall_us"), e.get("status_pushed_wall_us")
            if isinstance(lwe, (int, float)) and isinstance(sp, (int, float)) and sp > lwe:
                eos = round((sp - lwe) / 1000)
                derived_eos = True

        total = ac if ac else (eos or 0) + aud
        segs = []

        front = total - aud  # pre-model: turn detection (+ ASR poll)
        if front > 0:
            if eos and front >= eos:
                segs.append({"key": "turn_detection", "label": "turn detection", "ms": eos})
                gap = round(front - eos)
                if gap > 0:
                    segs.append({"key": "poll", "label": "ASR poll", "ms": gap})
            else:
                segs.append({"key": "turn_detection", "label": "turn detection", "ms": round(front)})

        if lat and utt and utt >= lat and aud >= utt:
            segs.append({"key": "ttft", "label": "model TTFT", "ms": lat})
            if utt - lat > 0:
                segs.append({"key": "model_utt", "label": "model→utterance", "ms": utt - lat})
            if aud - utt > 0:
                segs.append({"key": "utt_audio", "label": "utterance→audio", "ms": aud - utt})
        else:
            segs.append({"key": "model", "label": "model + TTS", "ms": aud})

        rows.append({
            "idx": idx,
            "seek": _seek(e, record_start),
            "text": (e.get("content") or "").strip()[:80],
            "segments": segs,
            "total": round(total),
            "audio": aud,
            "acoustic": ac,
            "eos": eos,
            "derived_eos": derived_eos,
        })
    return rows


# --------------------------------------------------------------------------- #
# Functions (the Functions tab — from swaig_log)
# --------------------------------------------------------------------------- #

def build_functions(payload: dict) -> list:
    """SWAIG calls with parsed args, results, and post_response actions."""
    out = []
    # tool entries carry execution latency keyed (loosely) by order
    tool_entries = [e for e in (payload.get("call_log") or []) if e.get("role") == "tool"]
    for i, s in enumerate(payload.get("swaig_log") or []):
        latency = {}
        if i < len(tool_entries):
            te = tool_entries[i]
            latency = {
                k: te[k]
                for k in ("latency", "function_latency", "execution_latency")
                if isinstance(te.get(k), (int, float))
            }
        out.append({
            "name": s.get("command_name"),
            "args": _loads(s.get("command_arg")),
            "epoch_time": s.get("epoch_time"),
            "ts_str": fmt_ts(int(s["epoch_time"]) * 1_000_000) if s.get("epoch_time") else "",
            "url": s.get("url"),
            "active_count": s.get("active_count"),
            "post_response": s.get("post_response"),
            "result": (tool_entries[i].get("content") if i < len(tool_entries) else None),
            "latency": latency,
        })
    return out


# --------------------------------------------------------------------------- #
# Totals / telemetry (the Overview + Telemetry tabs)
# --------------------------------------------------------------------------- #

def totals(payload: dict) -> dict:
    ai_start, ai_end = payload.get("ai_start_date"), payload.get("ai_end_date")
    minutes = payload.get("total_minutes")
    ws, we = _call_window(payload)
    return {
        "duration_s": (we - ws) / 1_000_000 if ws is not None else None,
        "ai_duration_s": (ai_end - ai_start) / 1_000_000 if (ai_start and ai_end and ai_end > ai_start) else None,
        "total_minutes": minutes,
        "input_tokens": payload.get("total_input_tokens"),
        "output_tokens": payload.get("total_output_tokens"),
        "wire_input_tokens": payload.get("total_wire_input_tokens"),
        "wire_output_tokens": payload.get("total_wire_output_tokens"),
        "wire_input_tpm": payload.get("total_wire_input_tokens_per_minute"),
        "wire_output_tpm": payload.get("total_wire_output_tokens_per_minute"),
        "tts_chars": payload.get("total_tts_chars"),
        "tts_chars_per_min": payload.get("total_tts_chars_per_min"),
        "asr_minutes": payload.get("total_asr_minutes"),
        "asr_cost_factor": payload.get("total_asr_cost_factor"),
        "est_ai_cost_usd": (minutes * AI_RUNTIME_USD_PER_MIN) if isinstance(minutes, (int, float)) else None,
    }


def summary(payload: dict) -> dict:
    """The AI's own post-call summary (post_prompt_data)."""
    ppd = payload.get("post_prompt_data") or {}
    return {
        "raw": ppd.get("raw"),
        "substituted": ppd.get("substituted"),
        "parsed": ppd.get("parsed"),
    }


# --------------------------------------------------------------------------- #
# Full-text search payload
# --------------------------------------------------------------------------- #

def transcript_text(payload: dict) -> str:
    """Flat text of the conversation for FTS indexing."""
    parts = []
    for e in payload.get("call_log") or []:
        if e.get("role") in _CONTENT_ROLES and e.get("content"):
            parts.append(e["content"])
        elif e.get("role") == "tool" and e.get("content"):
            parts.append(e["content"])
    ppd = (payload.get("post_prompt_data") or {}).get("raw")
    if ppd:
        parts.append(ppd)
    return "\n".join(parts)
