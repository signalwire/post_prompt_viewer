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


def test_latency_breakdown_segments_sum_to_total():
    payload = {
        "call_log": [
            # acoustic measured, no eos field -> unattributed turn-detection front
            {"role": "assistant", "content": "a", "latency": 150,
             "utterance_latency": 500, "audio_latency": 600, "acoustic_latency": 900},
            # eos derived from raw timestamps (300 ms); no acoustic
            {"role": "assistant", "content": "b", "latency": 100,
             "utterance_latency": 400, "audio_latency": 700,
             "last_word_end_wall_us": 1_000_000_000, "status_pushed_wall_us": 1_000_300_000},
            # filler / manual_say with no audio timing -> skipped
            {"role": "assistant-manual", "content": "filler", "audio_latency": 0,
             "acoustic_latency": 0},
        ]
    }
    rows = enrich.latency_breakdown(payload)
    assert len(rows) == 2  # the manual filler turn is skipped
    for r in rows:
        assert sum(s["ms"] for s in r["segments"]) == r["total"]
    assert rows[0]["total"] == 900  # = acoustic_latency
    assert rows[1]["eos"] == 300 and rows[1]["derived_eos"] is True
    assert rows[1]["total"] == 1000  # = eos + audio_latency
