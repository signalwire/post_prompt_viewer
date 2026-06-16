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
    "inner_dialog_scorecard": "Inner-dialog read",
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
    """Recording t=0 as a float (micros), tolerating the stringified form.

    Prefer ``record_first_frame`` — the wall-clock µs of the first PCM frame
    actually written to the recording file, on the same clock as every
    ``stamps_us``. Fall back to ``record_call_start`` (the relay-ack, which fires
    ~100 ms *before* the first frame) only when the newer field is absent, e.g.
    older calls or a call with no recording. Anchoring on the relay-ack shifts the
    whole recording timeline ~100 ms early and shows up as phantom "first_audio is
    late"; ``record_first_frame`` removes it without touching any stamp.
    """
    sv = payload.get("SWMLVars") or {}
    rs = sv.get("record_first_frame")
    if rs is None:
        rs = sv.get("record_call_start")
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


_LN_DIRECTIVE = re.compile(r"~LN\([^)]*\)-;\s*")


def clean_text(value: Any) -> Any:
    """Strip inline TTS language directives like ``~LN(English)-;`` from text;
    pass non-strings through unchanged."""
    if not isinstance(value, str):
        return value
    return _LN_DIRECTIVE.sub("", value)


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
    barge_idx = _barge_index(payload)

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
        content = clean_text(e.get("content"))

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
            barge = barge_idx.get((content or "").strip())
            if barge:
                turn["barge"] = barge
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
            turn["cat"] = EVENT_CAT.get(e.get("action"), "event")
            if e.get("action") == "inner_dialog_scorecard":
                turn["scorecard"] = _scorecard_metrics(parse_scorecard_text(content))

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
            "details": {k: clean_text(v) for k, v in details.items() if v not in (None, "")},
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
            point = {
                "ts": e.get("timestamp"),
                "start_ts": e.get("start_timestamp"),
                "text": (e.get("content") or "").strip()[:80],
                **tiers,
            }
            # Stamp-derived acoustic gap: first_audio - last_word_end, the
            # caller-stop -> AI-audio interval a wav analyzer measures. Robust to
            # the ``acoustic_latency`` *field*, which has been seen mis-anchored on
            # turn_decided (short by the eos->push gap). When the field is correct
            # the two agree to a frame.
            su = e.get("stamps_us") or {}
            fa = su.get("first_audio")
            lwe = e.get("last_word_end_wall_us") or su.get("last_word_end")
            if fa and lwe:
                try:
                    point["acoustic_stamp"] = round((float(fa) - float(lwe)) / 1000)
                except (TypeError, ValueError):
                    pass
            points.append(point)

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
    audio/acoustic latency. The reference is the stamp-derived acoustic gap
    (``first_audio - last_word_end``) — the same caller-stop -> AI-audio interval
    the wav measures — which is robust to the ``acoustic_latency`` field's anchor;
    that field is carried alongside for comparison and falls back in if a stamp is
    missing.
    """
    server = latency_series(payload)
    spoints = server["points"]
    wav = analysis.get("latencies") or []
    record_start = _record_start(payload)

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
                # Prefer the stamp-derived acoustic gap (first_audio -
                # last_word_end); fall back to the field, then audio_latency.
                ref = p.get("acoustic_stamp")
                if ref is None:
                    ref = p.get("acoustic_latency")
                if ref is None:
                    ref = p.get("audio_latency")
                pairs.append({
                    "wav_ms": wav_ms,
                    "audio_latency": p.get("audio_latency"),
                    "acoustic_latency": p.get("acoustic_latency"),
                    "acoustic_stamp": p.get("acoustic_stamp"),
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
    # Prefer the stamp-derived acoustic gap for the aggregate too; fall back to the
    # (possibly mis-anchored) field stat when no turn carries the stamps.
    astamp = [p["acoustic_stamp"] for p in spoints if p.get("acoustic_stamp") is not None]
    aggregate = {
        "wav_avg": _ms(stats.get("avg_latency")),
        "wav_median": _ms(stats.get("median_latency")),
        "wav_p95": _ms(stats.get("p95_latency")),
        "wav_count": stats.get("num_latencies"),
        "server_audio_avg": _rnd(audio, "avg"),
        "server_audio_median": _rnd(audio, "median"),
        "server_audio_p95": _rnd(audio, "p95"),
        "server_acoustic_avg": (round(sum(astamp) / len(astamp)) if astamp else _rnd(acoustic, "avg")),
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


def _speed(ms):
    """Speed bucket for colour/feel: fast ⚡ / ok / slow."""
    if not isinstance(ms, (int, float)):
        return "na"
    return "fast" if ms < 1500 else ("ok" if ms < 3000 else "slow")


def _verdict(user_entry: dict, det_ms: float):
    """Plain-English turn-taking verdict + kind, from the DG end-of-turn telemetry.

    The "cool" line: did it snap because it *knew*, or hold patiently without
    cutting the caller off, or get forced / uncertain.
    """
    eot = (user_entry or {}).get("eot") or {}
    basis = eot.get("basis")
    conf = eot.get("confidence")
    conf_pct = conf * 100 if isinstance(conf, (int, float)) else None
    timing = (user_entry or {}).get("timing") or {}
    segs = timing.get("segments")
    entity = (user_entry or {}).get("entity") or {}
    secs = "%.1fs" % (det_ms / 1000) if det_ms else None

    if basis == "entity_snap":
        what = entity.get("type")
        return ("snap", "snapped the instant it had a valid %s" % what if what
                else "snapped on a complete entity")
    if basis == "natural":
        return ("instant", "clean finish — committed instantly")
    if basis == "ceiling":
        return ("forced", "forced at the hold ceiling — may have clipped the caller")
    if basis == "growth_stop":
        if isinstance(segs, (int, float)) and segs > 1:
            return ("held", "let the caller finish all %d parts — never cut in (%s)"
                    % (int(segs), secs or "held"))
        if conf_pct is not None and conf_pct < 50:
            return ("uncertain", "shaky endpoint, %.0f%% — held %s" % (conf_pct, secs or "briefly"))
        return ("held", "held %s to be sure — no cutoff" % (secs or "briefly"))
    return (None, None)


# Canonical wall-clock pipeline events for an AI turn, in causal order. Each is
# read from the stamps_us block, falling back to the matching *_wall_us field.
# They all share one clock (switch_time_now), so they lay out on a single axis.
_PIPELINE_EVENTS = [
    ("speech_start",    "speech_start_wall_us",    "caller started speaking"),
    ("last_word_end",   "last_word_end_wall_us",   "caller's last word"),
    ("turn_decided",    "turn_decided_wall_us",    "end-of-turn decided"),
    ("status_pushed",   "status_pushed_wall_us",   "status pushed"),
    ("request_detect",  None,                      "LLM request dispatched"),
    ("first_token",     None,                      "first LLM token"),
    ("first_utterance", None,                      "first TTS utterance"),
    ("first_audio",     None,                      "first audio to caller"),
]


def _word_count(text: Optional[str]) -> Optional[int]:
    return len(text.split()) if isinstance(text, str) and text.strip() else None


def _barge(entry: dict) -> Optional[dict]:
    """If the caller barged in over this AI turn: when, and how much was heard.

    ``text_heard_approx`` is the portion the caller actually heard before cutting
    in; ``text_spoken_total`` is the full reply the agent intended. We surface the
    fraction heard and the unheard remainder (when ``heard`` is a clean prefix).
    """
    if not entry.get("barged"):
        return None
    heard = entry.get("text_heard_approx")
    total = entry.get("text_spoken_total")
    hw, tw = _word_count(heard), _word_count(total)
    unheard = None
    if isinstance(heard, str) and isinstance(total, str) and total.startswith(heard):
        unheard = total[len(heard):]
    return {
        "elapsed_ms": _pos(entry.get("barge_elapsed_ms")),
        "heard": heard,
        "total": total,
        "unheard": unheard,
        "heard_words": hw,
        "total_words": tw,
        "pct_heard": round(100 * hw / tw) if (hw and tw) else None,
    }


def _barge_index(payload: dict) -> dict:
    """Map spoken text -> barge info. The barge fields ride the ``raw_call_log`` /
    ``call_timeline`` entries (the "raw_array"), NOT the collapsed ``call_log``, so
    we index them by the spoken text to reattach them to the blessed turns. Keyed
    by ``text_spoken_total`` / ``content`` (stripped), which match the blessed
    assistant ``content`` verbatim.
    """
    index = {}
    sources = list(payload.get("raw_call_log") or [])
    sources += [e for e in (payload.get("call_timeline") or []) if e.get("type") == "ai_response"]
    for e in sources:
        if not isinstance(e, dict) or not e.get("barged"):
            continue
        info = _barge(e)
        if not info:
            continue
        for key in (e.get("text_spoken_total"), e.get("content"), e.get("text_heard_approx")):
            if isinstance(key, str) and key.strip():
                index.setdefault(key.strip(), info)
    return index


# Milestone -> visual category (colour) and human label, for the trace timeline.
_MILESTONE_CAT = {
    "speech_start": "caller", "last_word_end": "caller",
    "turn_decided": "detect", "status_pushed": "detect",
    "request_detect": "llm", "first_token": "llm",
    "first_utterance": "tts", "first_audio": "audio",
    "filler_audio": "filler", "tool_start": "tool", "tool_end": "tool",
}
_MILESTONE_LABEL = {
    "speech_start": "caller started", "last_word_end": "caller's last word",
    "turn_decided": "turn detected", "status_pushed": "status pushed",
    "request_detect": "LLM dispatched", "first_token": "first token",
    "first_utterance": "first utterance", "first_audio": "first audio",
    "filler_audio": "filler audio", "tool_start": "function start", "tool_end": "function done",
}
# Label for the span *leading into* each milestone (the gap before it).
_GAP_LABEL = {
    "last_word_end": "caller speaking", "turn_decided": "end-of-turn hold",
    "status_pushed": "queue push", "filler_audio": "to filler audio",
    "tool_start": "dispatch", "tool_end": "function", "request_detect": "tool / wait",
    "first_token": "LLM first token", "first_utterance": "TTS", "first_audio": "audio out",
}


def _stamps_of(entry: dict) -> dict:
    """Wall-clock stamps present on a call_log entry (stamps_us + *_wall_us)."""
    su = entry.get("stamps_us") or {}
    out = {}
    for name, wall_key, _label in _PIPELINE_EVENTS:
        v = su.get(name)
        if v is None and wall_key:
            v = entry.get(wall_key)
        if isinstance(v, (int, float)) and v > 0:
            out[name] = int(v)
    return out


def build_trace(payload: dict) -> list:
    """One trace per exchange (caller turn → AI reply), the whole conversation.

    Every wall-clock milestone is mapped onto a single time axis; the gaps
    between them are the labelled breakdown; SWAIG calls are spans you can open
    for args + result; and the headline is the mouth-to-ear turn latency (the
    caller's last word → first audio). Renders every turn, stamps or not.
    """
    call_log = payload.get("call_log") or []
    record_start = _record_start(payload)
    funcs = build_functions(payload)
    ppd = payload.get("post_prompt_data") or {}
    summary_texts = {(ppd.get(k) or "").strip() for k in ("raw", "substituted")} - {""}

    # Group the log into exchanges: a user turn and the AI activity it triggered
    # (fillers, tool results, the spoken reply) up to the next user turn. AI turns
    # before any user turn (the greeting) form a leading exchange with no caller.
    # system / system-log entries (step changes, session end) don't match any branch
    # so they never perturb grouping; the post-call summary is not a turn either.
    groups, cur = [], None
    for e in call_log:
        role = e.get("role")
        if role == "user" and e.get("content"):
            if cur:
                groups.append(cur)
            cur = {"user": e, "ai": [], "tools": []}
        elif role in ("assistant", "assistant-manual") and e.get("content"):
            if (e.get("content") or "").strip() in summary_texts:
                continue
            cur = cur or {"user": None, "ai": [], "tools": []}
            cur["ai"].append(e)
        elif role == "tool":
            cur = cur or {"user": None, "ai": [], "tools": []}
            cur["tools"].append(e)
    if cur:
        groups.append(cur)

    out, fpi = [], 0
    for g in groups:
        u = g["user"]
        pts = []  # (t_us, name, cat)
        if u:
            su = _stamps_of(u)
            for n in ("speech_start", "last_word_end", "turn_decided", "status_pushed"):
                if n in su:
                    pts.append((su[n], n, _MILESTONE_CAT[n]))
        reply = None
        for ai in g["ai"]:
            su = _stamps_of(ai)
            if ai.get("role") == "assistant-manual":
                if "first_audio" in su:
                    pts.append((su["first_audio"], "filler_audio", "filler"))
            else:
                reply = ai
                for n in ("request_detect", "first_token", "first_utterance", "first_audio"):
                    if n in su:
                        pts.append((su[n], n, _MILESTONE_CAT[n]))

        tools = []
        for t in g["tools"]:
            sw = funcs[fpi] if fpi < len(funcs) else None
            fpi += 1
            st, en = _intish(t.get("start_timestamp")), _intish(t.get("end_timestamp"))
            tools.append({
                "name": t.get("function_name") or (sw or {}).get("name") or "function",
                "ms": (_pos(t.get("execution_latency")) or _pos(t.get("function_latency"))
                       or _pos(t.get("latency"))),
                "args": (sw or {}).get("args"),
                "result": clean_text((sw or {}).get("result") or t.get("content")),
                "url": (sw or {}).get("url"),
                "post_response": (sw or {}).get("post_response"),
            })
            if isinstance(st, int):
                pts.append((st, "tool_start", "tool"))
            if isinstance(en, int):
                pts.append((en, "tool_end", "tool"))

        # The reply text / seek even when no stamps were captured for the exchange.
        reply_e = reply or (g["ai"][-1] if g["ai"] else None)
        row = {
            "caller": clean_text((u.get("content") or "").strip()) if u else None,
            "reply": clean_text((reply_e.get("content") or "").strip()) if reply_e else "",
            "seek": _seek(reply_e or u or {}, record_start),
            "tools": tools,
            "milestones": [],
            "stages": [],
            "span_ms": None,
            "hero_ms": None,
            "speed": "na",
            "talk_ms": None,
        }
        if u:
            ent = u.get("entity") if isinstance(u.get("entity"), dict) else None
            eot = u.get("eot") or {}
            row["caller_meta"] = {
                "entity": ent if (ent and ent.get("value")) else None,
                "eot": eot.get("basis"),
                "confidence": (u.get("confidence") * 100
                               if isinstance(u.get("confidence"), (int, float)) else None),
            }
            su_u = _stamps_of(u)
            det_ms = (round((su_u["turn_decided"] - su_u["last_word_end"]) / 1000)
                      if ("last_word_end" in su_u and "turn_decided" in su_u) else 0)
            if not det_ms:
                cl = (u.get("timing") or {}).get("commit_latency_ms")
                det_ms = cl if isinstance(cl, (int, float)) else 0
            row["verdict_kind"], row["verdict"] = _verdict(u, det_ms)

        if pts:
            seen, dedup = set(), []
            for pt in pts:
                if (pt[0], pt[1]) in seen:
                    continue
                seen.add((pt[0], pt[1]))
                dedup.append(pt)
            pts = sorted(dedup, key=lambda x: x[0])
            t0, t1 = pts[0][0], pts[-1][0]
            span = max(1, t1 - t0)
            row["span_ms"] = round(span / 1000)
            row["milestones"] = [{
                "name": n, "label": _MILESTONE_LABEL.get(n, n), "cat": cat,
                "off_ms": round((t - t0) / 1000), "x": round((t - t0) / span * 100, 2),
                "ts": fmt_ts(t, "%H:%M:%S"),
            } for (t, n, cat) in pts]
            stages = []
            for (ta, _na, _ca), (tb, nb, cb) in zip(pts, pts[1:]):
                ms = round((tb - ta) / 1000)
                if ms < 1:
                    continue
                stages.append({
                    "label": _GAP_LABEL.get(nb, nb), "cat": cb, "ms": ms,
                    "x": round((ta - t0) / span * 100, 2),
                    "w": round((tb - ta) / span * 100, 2),
                    "tool": nb == "tool_end",
                })
            row["stages"] = stages
            # headline: mouth-to-ear (caller's last word → first audio out)
            lwe = next((t for (t, n, _c) in pts if n == "last_word_end"), None)
            fa = next((t for (t, n, _c) in reversed(pts) if n in ("first_audio", "filler_audio")), None)
            sstart = next((t for (t, n, _c) in pts if n == "speech_start"), None)
            row["hero_ms"] = round((fa - lwe) / 1000) if (lwe and fa) else round(span / 1000)
            row["speed"] = _speed(row["hero_ms"])
            row["anchored"] = lwe is not None
            if sstart and lwe:
                row["talk_ms"] = round((lwe - sstart) / 1000)
        out.append(row)
    return out


_WAVE_MS_LABELS = {"turn_decided": "EOT", "first_token": "token", "first_audio": "audio"}


def wave_markers(payload: dict) -> list:
    """Recording-relative markers for the waveform overlay: SWAIG tool spans and
    the key per-turn pipeline stamps, so the milestones scroll with the audio.
    ``t`` / ``dur`` are seconds from the start of the recording.
    """
    rs = _record_start(payload)
    if not rs:
        return []
    out = []
    for e in payload.get("call_log") or []:
        role = e.get("role")
        if role == "tool":
            st, en = e.get("start_timestamp"), e.get("end_timestamp")
            try:
                t0 = (float(st) - rs) / 1e6 if st else None
                dur = (float(en) - float(st)) / 1e6 if (st and en) else 0
            except (TypeError, ValueError):
                t0, dur = None, 0
            if t0 is not None and t0 >= 0:
                out.append({"t": round(t0, 3), "dur": round(max(0, dur), 3),
                            "label": "ƒ " + (e.get("function_name") or "function"), "kind": "tool"})
        elif role in ("assistant", "assistant-manual") and e.get("content"):
            stamps = _stamps_of(e)
            for name, lab in _WAVE_MS_LABELS.items():
                if name in stamps:
                    t = (stamps[name] - rs) / 1e6
                    if t >= 0:
                        out.append({"t": round(t, 3), "dur": 0, "label": lab, "kind": name})
    out.sort(key=lambda m: m["t"])
    return out


def build_waterfall(payload: dict) -> dict:
    """Every call_timeline event on a real time axis — the "every second" debug
    view. Returns ``{events: [...], span}`` where each event has a lane, an offset
    (ms from call start) and a duration (ms) for the bar, plus its metadata.
    """
    events = build_timeline(payload)
    tss = [e["ts"] for e in events if e.get("ts")]
    if not tss:
        return {"events": [], "span": 1}
    start = min(tss)
    span = max(1, round((max(tss) - start) / 1000))
    lanes = {
        "user_input": "human", "ai_response": "ai", "tool_result": "tool",
        "function_call": "tool", "gather_start": "gather", "gather_question": "gather",
        "gather_answer": "gather", "step_change": "step", "context_enter": "step",
        "session_start": "meta", "session_end": "meta",
    }
    funcs = build_functions(payload)
    fi = 0
    out = []
    for e in events:
        ts = e.get("ts")
        if not ts:
            continue
        d = e.get("details") or {}
        etype = e["type"]
        off = (ts - start) / 1000
        dur = 0
        swaig = None
        if etype == "user_input":
            st, en = d.get("start_timestamp"), d.get("end_timestamp")
            try:
                if st and en:
                    off = (float(st) - start) / 1000
                    dur = (float(en) - float(st)) / 1000
            except Exception:
                pass
        elif etype == "tool_result":
            ex = d.get("execution_latency") or d.get("latency")
            if isinstance(ex, (int, float)):
                off, dur = max(0, off - ex), ex
        elif etype == "ai_response":
            dur = d.get("audio_latency") or 0
        elif etype == "function_call":
            dur = d.get("duration_ms") or 0
            if fi < len(funcs):
                swaig = funcs[fi]
                fi += 1
        out.append({
            "type": etype,
            "lane": lanes.get(etype, "meta"),
            "label": e["label"],
            "ts_str": e["ts_str"],
            "elapsed": e["elapsed"],
            "off": round(max(0, off)),
            "dur": round(dur),
            "details": d,
            "swaig": swaig,
        })
    out.sort(key=lambda ev: ev["off"])
    return {"events": out, "span": span}


# --------------------------------------------------------------------------- #
# Event stream (the Events sub-tab — the call's control flow as a narrative)
# --------------------------------------------------------------------------- #

# Conversational turns are shown in Conversation / Pipeline / Flow; the Events
# narrative is the *control flow* around them.
_EVENT_SKIP = {"user_input", "ai_response", "tool_result"}

# Event type -> visual category (drives the rail dot colour).
EVENT_CAT = {
    "session_start": "session", "session_end": "session", "summarize_start": "session",
    "startup_hook": "session", "hangup_hook": "session",
    "step_change": "nav", "context_enter": "nav", "reset": "nav",
    "gather_start": "gather", "gather_question": "gather", "gather_answer": "gather",
    "gather_reject": "error", "gather_complete": "gather",
    "function_call": "fn", "function_error": "error",
    "check_for_input": "misc", "attention_timeout": "misc",
    "inner_dialog_scorecard": "score",
    "manual_say": "say", "filler": "say",
    "hearing_hint": "edit", "pronounce_rule": "edit", "pronounce": "edit",
    "auto_correct": "edit", "text_normalize": "edit",
}


def _event_title(etype: str, d: dict) -> str:
    """One human-readable headline for a control-flow event."""
    g = d.get
    def ms(x):
        return " · %dms" % x if isinstance(x, (int, float)) and x else ""
    if etype == "session_start":
        return "Session started" + (" · %s" % g("model") if g("model") else "")
    if etype == "session_end":
        return "Session ended — %s%s" % (g("reason") or "?", " by %s" % g("ended_by") if g("ended_by") else "")
    if etype == "summarize_start":
        return "Summarizing conversation" + (" · %s" % g("model") if g("model") else "")
    if etype in ("startup_hook", "hangup_hook"):
        return EVENT_LABELS.get(etype, etype) + ms(g("duration_ms")) + (" — %s" % g("error") if g("error") else "")
    if etype == "step_change":
        return "Step  %s → %s" % (g("from_step") or "?", g("to_step") or "?")
    if etype == "context_enter":
        return "Context  %s → %s" % (g("from_context") or "?", g("to_context") or "?")
    if etype == "reset":
        return "Conversation reset" + (" (full)" if g("full_reset") else "")
    if etype == "gather_start":
        return "Gather started" + (" — %d questions" % g("total_questions") if g("total_questions") else "")
    if etype == "gather_question":
        return "Asking: %s%s" % (g("key") or "?", " (%s)" % g("question_type") if g("question_type") else "")
    if etype == "gather_answer":
        return "Answered: %s%s" % (g("key") or "?", " · confirmed" if g("confirmed") else "")
    if etype == "gather_reject":
        return "Rejected: %s — %s" % (g("key") or "?", g("reason") or "?")
    if etype == "gather_complete":
        return "Gather complete" + (" — %s answered" % g("answered") if g("answered") is not None else "")
    if etype == "function_call":
        return "%s()%s%s" % (g("function") or "?", ms(g("duration_ms")), " · native" if g("native") else "")
    if etype == "function_error":
        return "Function error — %s (%s)" % (g("function") or "?", g("error_type") or g("http_code") or "?")
    if etype == "check_for_input":
        return "Input poll returned" + ms(g("duration_ms"))
    if etype == "attention_timeout":
        return "Attention timeout" + ms(g("timeout_ms"))
    if etype == "manual_say":
        return "System said" + (" — recovery" if g("is_error") else "")
    if etype == "filler":
        return "Filler audio" + (" · %s" % g("filler_type") if g("filler_type") else "")
    return EVENT_LABELS.get(etype, etype)


def _event_extra(etype: str, d: dict) -> str:
    """Secondary detail line (verbatim text, a rewrite, an error reason)."""
    g = d.get
    if etype in ("manual_say", "filler"):
        t, r = g("text"), g("error_reason")
        return (("“%s”" % t) if t else "") + ((" — %s" % r) if r else "")
    if etype in ("hearing_hint", "pronounce_rule", "pronounce", "auto_correct", "text_normalize"):
        o = clean_text(g("original"))
        r = clean_text(g("result") or g("corrected") or g("normalized"))
        return ("%s → %s" % (o, r)) if (o or r) else ""
    if etype == "step_change" and g("trigger"):
        return "trigger: %s" % g("trigger")
    if etype == "function_call" and g("error"):
        return "error: %s" % g("error")
    if etype == "gather_answer" and g("attempt"):
        return "attempt %s" % g("attempt")
    return ""


_SCORECARD_INVERT = {"frustration"}
_SCORECARD_SKIP = {"v"}


def _scorecard_metrics(card: dict) -> list:
    """Render-ready scorecard rows: numeric metrics (0-1) become coloured bars,
    text metrics (expertise / intent / qualification) become chips. ``good`` is
    0..1 where 1 is the favourable end (inverted for frustration)."""
    out = []
    for k, v in (card or {}).items():
        if k in _SCORECARD_SKIP or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            val = max(0.0, min(1.0, float(v)))
            good = (1 - val) if k in _SCORECARD_INVERT else val
            out.append({"key": k, "label": k.replace("_", " "), "kind": "num",
                        "pct": round(val * 100), "good": round(good, 3)})
        elif v not in (None, ""):
            out.append({"key": k, "label": k.replace("_", " "), "kind": "text", "text": str(v)})
    return out


def parse_scorecard_text(text: str) -> dict:
    """Parse an ``inner_dialog_scorecard`` content blob (``- key: value`` lines)."""
    card = {}
    for line in (text or "").splitlines():
        m = re.match(r"\s*-\s*([a-z_]+)\s*:\s*(.+)", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        try:
            card[key] = float(raw)
        except ValueError:
            card[key] = raw
    return card


def scorecard(payload: dict) -> Optional[dict]:
    """The final qualification scorecard from ``global_data`` as render-ready
    metrics, or None when the call didn't run inner dialog."""
    card = (payload.get("global_data") or {}).get("scorecard")
    if not isinstance(card, dict) or not card:
        return None
    return {"metrics": _scorecard_metrics(card), "raw": card}


def build_events(payload: dict) -> list:
    """The call's control flow as a readable narrative — sourced straight from the
    ``call_log`` system-log entries so EVERY one shows (the producer's
    ``call_timeline`` silently drops entries that carry no metadata, e.g. the
    inner-dialog scorecard reads). Step/context changes, gather flow, function
    calls + errors, session lifecycle, attention timeouts, and inner-dialog
    scorecards; text rewrites are skipped (their own feature).
    """
    out = []
    last_ts = None
    for e in payload.get("call_log") or []:
        if e.get("role") != "system-log":
            continue
        etype = e.get("action") or "event"
        if EVENT_CAT.get(etype) == "edit":
            continue
        d = e.get("metadata") or {}
        ts = e.get("timestamp")
        item = {
            "ts_str": fmt_ts(ts) if ts else "",
            "elapsed": fmt_elapsed(last_ts, ts) if (ts and last_ts) else "",
            "type": etype,
            "cat": EVENT_CAT.get(etype, "misc"),
            "title": _event_title(etype, d),
            "extra": _event_extra(etype, d),
        }
        if etype == "inner_dialog_scorecard":
            item["scorecard"] = _scorecard_metrics(parse_scorecard_text(e.get("content")))
        out.append(item)
        if ts:
            last_ts = ts
    return out


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


def _intish(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def call_facts(payload: dict) -> dict:
    """All the call-level metadata for the Data tab — identity, caller/routing,
    recording, and timing — drawn from the top-level fields, ``SWMLVars`` and
    ``SWMLCall``, plus those two blocks verbatim for "everything else captured".
    Each fact is ``(label, value, kind)`` where kind drives rendering
    (mono id / timestamp / url / plain).
    """
    sv = payload.get("SWMLVars") or {}
    sc = payload.get("SWMLCall") or {}
    groups = {
        "identity": [
            ("Call ID", payload.get("call_id"), "mono"),
            ("AI session", payload.get("ai_session_id"), "mono"),
            ("AI id tag", payload.get("ai_id_tag"), "mono"),
            ("Segment", sc.get("segment_id"), "mono"),
            ("Project", payload.get("project_id") or sc.get("project_id"), "mono"),
            ("Space", payload.get("space_id") or sc.get("space_id"), "mono"),
            ("Node", sc.get("node_id"), "mono"),
            ("App", payload.get("app_name"), "text"),
            ("Action", payload.get("action"), "text"),
            ("Disposition", payload.get("content_disposition"), "text"),
        ],
        "routing": [
            ("Caller name", payload.get("caller_id_name"), "text"),
            ("Caller number", payload.get("caller_id_number"), "mono"),
            ("Direction", sc.get("direction"), "text"),
            ("From", sc.get("from"), "mono"),
            ("To", sc.get("to"), "mono"),
            ("State", sc.get("call_state"), "text"),
            ("Channel", sc.get("type"), "text"),
            ("Conversation", payload.get("conversation_type"), "text"),
        ],
        "recording": [
            ("Recording URL", sv.get("record_call_url"), "url"),
            ("Record first frame", _intish(sv.get("record_first_frame")), "ts"),
            ("Record start (relay-ack)", _intish(sv.get("record_call_start")), "ts"),
            ("Record result", sv.get("record_call_result"), "text"),
            ("Answer result", sv.get("answer_result"), "text"),
            ("Control ID", sv.get("record_control_id"), "mono"),
        ],
        "timing": [
            ("Call start", payload.get("call_start_date"), "ts"),
            ("Answered", payload.get("call_answer_date"), "ts"),
            ("AI start", payload.get("ai_start_date"), "ts"),
            ("AI end", payload.get("ai_end_date"), "ts"),
            ("Call end", payload.get("call_end_date"), "ts"),
        ],
    }
    out = {g: [(label, v, k) for (label, v, k) in rows if v not in (None, "", [], {})]
           for g, rows in groups.items()}
    out["swml_vars"] = sv
    out["swml_call"] = sc
    return out


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
