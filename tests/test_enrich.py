import json
from pathlib import Path

import pytest

from post_prompt_viewer import enrich

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "samples"
    / "conversation_demo.json"
)


@pytest.fixture(scope="module")
def payload():
    return json.loads(SAMPLE.read_text())


def test_derive_index(payload):
    idx = enrich.derive_index(payload, received_at_us=1)
    assert idx["call_id"] == "00000000-0000-4000-8000-000000000001"
    assert idx["app_name"] == "demo agent"
    assert idx["has_recording"] is True
    assert idx["num_user_turns"] == 2
    assert idx["num_assistant_turns"] == 3
    assert idx["duration_s"] == pytest.approx(90.0, abs=0.1)
    assert idx["avg_latency_ms"] == pytest.approx(900.0, abs=0.5)


def test_transcript_perf_matched_by_text(payload):
    turns = enrich.build_transcript(payload)
    assert len(turns) == len(payload["call_log"])
    # The second spoken AI turn must match its real times[] entry by text,
    # not by order (a tool-call generation sits between turns).
    spoken = [t for t in turns if t["kind"] == "assistant" and t.get("perf")]
    assert spoken
    second = next(t for t in spoken if t["content"].strip().startswith("You can add funds"))
    assert second["perf"]["tps"] == pytest.approx(120.0, abs=0.5)


def test_transcript_tool_calls_parsed(payload):
    turns = enrich.build_transcript(payload)
    tc = [t for t in turns if t["kind"] == "tool-call"]
    assert tc and tc[0]["tool_calls"][0]["name"] == "search_docs"
    assert isinstance(tc[0]["tool_calls"][0]["arguments"], dict)


def test_raw_vs_blessed(payload):
    blessed = enrich.build_transcript(payload, source="blessed")
    raw = enrich.build_transcript(payload, source="raw")
    assert len(raw) == len(payload["raw_call_log"])
    assert len(raw) > len(blessed)


def test_latency_series(payload):
    ls = enrich.latency_series(payload)
    assert ls["stats"]["audio_latency"]["count"] == 3
    assert ls["has_acoustic"] is False


def test_functions(payload):
    fns = enrich.build_functions(payload)
    assert "search_docs" in [f["name"] for f in fns]
    assert isinstance(fns[0]["args"], dict)


def test_user_turn_telemetry():
    # entity extraction, end-of-turn basis/confidence, and ASR timing on a user turn
    payload = {"call_log": [{
        "role": "user", "content": "jordan.rivera at gmail.com", "timestamp": 2,
        "confidence": 0.997,
        "entity": {"type": "email", "valid": True, "value": "jordan.rivera@gmail.com"},
        "eot": {"basis": "entity_snap", "confidence": 0.97},
        "timing": {"commit_latency_ms": 398, "hold_ms": 398, "segments": 2},
        "speaking_to_final_event": 5520,
        "speaking_to_turn_detection": 4769,
        "turn_detection_to_final_event": 750,
    }]}
    t = enrich.build_transcript(payload)[0]
    assert t["entity"] == {"type": "email", "valid": True, "value": "jordan.rivera@gmail.com"}
    assert t["eot"]["basis"] == "entity_snap"
    assert t["eot"]["confidence"] == pytest.approx(97.0)
    assert t["asr"]["commit_latency_ms"] == 398
    assert t["asr"]["segments"] == 2
    assert t["asr"]["speaking_to_final_event"] == 5520
    assert t["asr"]["turn_detection_to_final_event"] == 750


def test_align_latency_synthetic():
    rec = 100_000_000
    payload = {
        "SWMLVars": {"record_call_start": str(rec)},  # stringified, as real payloads send it
        "call_log": [
            {
                "role": "assistant",
                "content": "hi",
                "timestamp": rec + 5_200_000,
                "start_timestamp": rec + 5_000_000,
                "audio_latency": 500,
                "acoustic_latency": 700,
            }
        ],
    }
    analysis = {
        "latencies": [{"human_stop": 4.4, "ai_start": 5.0, "latency": 0.68}],
        "statistics": {"avg_latency": 0.68, "num_latencies": 1},
    }
    out = enrich.align_latency(payload, analysis)
    assert out["matched"] == 1
    assert out["pairs"][0]["wav_ms"] == 680
    assert out["pairs"][0]["delta_ms"] == -20  # 680 - 700 (acoustic preferred)
    assert out["aggregate"]["server_acoustic_avg"] == 700


def test_build_waterfall_offsets_lanes_and_swaig():
    payload = {
        "call_log": [{"role": "assistant", "content": "hi", "timestamp": 2_000_000}],
        "call_timeline": [
            {"ts": 1_000_000, "type": "session_start", "step": "greet"},
            {"ts": 3_000_000, "type": "user_input", "start_timestamp": 1_500_000,
             "end_timestamp": 3_000_000, "confidence": 95.0},
            {"ts": 5_000_000, "type": "function_call", "function": "lookup", "duration_ms": 250},
        ],
        "swaig_log": [{"command_name": "lookup", "command_arg": "{\"q\": 1}",
                       "post_response": {"response": "ok"}}],
    }
    wf = enrich.build_waterfall(payload)
    assert wf["span"] == 4000  # (5_000_000 - 1_000_000) / 1000
    evs = {e["type"]: e for e in wf["events"]}
    assert evs["session_start"]["off"] == 0
    assert evs["user_input"]["lane"] == "human" and evs["user_input"]["dur"] == 1500
    assert evs["function_call"]["lane"] == "tool"
    assert evs["function_call"]["swaig"]["name"] == "lookup"
    assert [e["off"] for e in wf["events"]] == sorted(e["off"] for e in wf["events"])


def test_build_events_narrative_and_clean_text():
    payload = {"call_timeline": [
        {"ts": 1_000_000, "type": "session_start", "model": "gpt-4o"},
        {"ts": 2_000_000, "type": "step_change", "from_step": "greet", "to_step": "collect",
         "trigger": "ai_function"},
        {"ts": 3_000_000, "type": "function_call", "function": "check_order", "duration_ms": 234},
        {"ts": 4_000_000, "type": "function_error", "function": "check_order",
         "error_type": "timeout", "http_code": 500},
        {"ts": 5_000_000, "type": "ai_response", "audio_latency": 100},          # conversational -> skipped
        {"ts": 6_000_000, "type": "pronounce", "original": "~LN(English)-; Hi", "result": "Hi"},  # rewrite -> skipped
    ]}
    ev = enrich.build_events(payload)
    types = [e["type"] for e in ev]
    assert "ai_response" not in types and "pronounce" not in types
    by = {e["type"]: e for e in ev}
    assert by["session_start"]["cat"] == "session"
    assert "greet → collect" in by["step_change"]["title"]
    assert by["step_change"]["extra"] == "trigger: ai_function"
    assert by["function_call"]["title"].startswith("check_order() · 234ms")
    assert by["function_error"]["cat"] == "error"
    # the ~LN(*)-; TTS directive is stripped wherever text is shown
    assert enrich.clean_text("~LN(English)-; Hello there") == "Hello there"
    assert enrich.clean_text("plain text") == "plain text"


def test_call_facts_groups_and_swml():
    payload = {
        "call_id": "abc", "ai_session_id": "sess", "project_id": "proj", "app_name": "swml app",
        "caller_id_name": "tony", "caller_id_number": "+12025550100", "conversation_type": "voice",
        "call_start_date": 1781402455892315,
        "SWMLVars": {"record_call_url": "https://files/x.wav", "record_call_start": "1781402457172746",
                     "record_call_result": "success"},
        "SWMLCall": {"direction": "inbound", "from": "sip:+1@host", "to": "sip:bot@host",
                     "call_state": "answered", "type": "sip", "segment_id": "seg"},
    }
    cf = enrich.call_facts(payload)
    ident = {l: v for l, v, k in cf["identity"]}
    assert ident["Call ID"] == "abc" and ident["Project"] == "proj" and ident["Segment"] == "seg"
    rec = {l: (v, k) for l, v, k in cf["recording"]}
    assert rec["Recording URL"] == ("https://files/x.wav", "url")
    assert rec["Record start"] == (1781402457172746, "ts")  # stringified micros coerced to int
    routing = {l: v for l, v, k in cf["routing"]}
    assert routing["From"] == "sip:+1@host" and routing["Direction"] == "inbound"
    # empty fields are dropped; the raw blocks pass through verbatim
    assert all(v not in (None, "") for g in ("identity", "routing", "recording", "timing")
               for _, v, _ in cf[g])
    assert cf["swml_vars"]["record_call_result"] == "success" and cf["swml_call"]["type"] == "sip"
