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


def test_build_flow_split_and_held_verdict():
    # Latency = detection (commit) + tool (execution) + model, summing to the
    # total; pairing reaches the user turn across the tool-call entries; the eot
    # basis drives the plain-English verdict.
    payload = {"call_log": [
        {"role": "user", "content": "714 East Osage", "timestamp": 1,
         "timing": {"commit_latency_ms": 5000, "segments": 3},
         "eot": {"basis": "growth_stop", "confidence": 0.88}},
        {"role": "assistant", "timestamp": 2,
         "tool_calls": [{"id": "1", "type": "function", "function": {"name": "validate", "arguments": "{}"}}]},
        {"role": "tool", "content": "ok", "timestamp": 3,
         "function_name": "validate", "execution_latency": 1500},
        {"role": "assistant", "content": "The pickup address is set.", "timestamp": 4,
         "latency": 500, "utterance_latency": 600, "audio_latency": 700, "acoustic_latency": 9999},
    ]}
    f = enrich.build_flow(payload)[0]  # no recording -> server total = acoustic
    assert f["det"] + f["tool"] + f["model"] == f["total"] == 9999
    assert f["det"] == 5000 and f["tool"] == 1500 and f["total_source"] == "server"
    assert f["verdict_kind"] == "held" and "3 parts" in f["verdict"]
    assert f["human"]["text"] == "714 East Osage"


def test_build_flow_entity_snap_verdict():
    payload = {"call_log": [
        {"role": "user", "content": "j at gmail", "timestamp": 1,
         "timing": {"commit_latency_ms": 300},
         "entity": {"type": "email", "valid": True, "value": "j@gmail.com"},
         "eot": {"basis": "entity_snap", "confidence": 0.97}},
        {"role": "assistant", "content": "Got it.", "timestamp": 2,
         "latency": 200, "utterance_latency": 300, "audio_latency": 400, "acoustic_latency": 1200},
    ]}
    f = enrich.build_flow(payload)[0]
    assert f["verdict_kind"] == "snap" and "email" in f["verdict"]
    assert f["human"]["entity"]["value"] == "j@gmail.com"


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
