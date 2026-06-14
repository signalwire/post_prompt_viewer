import json
from pathlib import Path

import pytest

from post_prompt_viewer import storage

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "samples"
    / "conversation_demo.json"
)


@pytest.fixture
def sample():
    return json.loads(SAMPLE.read_text())


def test_save_get_idempotent(sample):
    storage.init_db()
    cid = storage.save_call(sample)
    assert cid == sample["call_id"]
    assert storage.count_calls() == 1
    storage.save_call(sample)  # re-ingest is an upsert
    assert storage.count_calls() == 1
    assert storage.get_call(cid)["payload"]["app_name"] == "demo agent"


def test_list_search_filter(sample):
    storage.init_db()
    storage.save_call(sample)
    assert storage.list_calls(q="billing")
    assert storage.list_calls(q="zzznope") == []
    assert storage.list_calls(has_recording=True)
    assert storage.list_calls(has_recording=False) == []
    assert "demo agent" in storage.distinct_apps()


def test_recordings_roundtrip(sample):
    storage.init_db()
    cid = storage.save_call(sample)
    assert storage.get_recording(cid)["status"] == "pending"
    storage.set_recording(cid, status="done", analysis={"x": 1}, duration_s=12.0)
    rec = storage.get_recording(cid)
    assert rec["status"] == "done"
    assert rec["analysis"] == {"x": 1}
    assert storage.pending_recordings() == []


def test_delete_call(sample):
    storage.init_db()
    cid = storage.save_call(sample)
    assert storage.count_calls() == 1
    assert storage.list_calls(q="billing")          # FTS row present
    assert storage.get_recording(cid) is not None    # recording row seeded

    rec = storage.delete_call(cid)
    assert rec is not None                            # returned the removed recording row
    assert storage.count_calls() == 0
    assert storage.get_call(cid) is None
    assert storage.get_recording(cid) is None
    assert storage.list_calls(q="billing") == []      # FTS cleaned up too
    assert storage.delete_call(cid) is None           # deleting an unknown id is a no-op


def test_summaries():
    storage.init_db()
    storage.append_summary("c1", "first")
    storage.append_summary("c1", "second")
    assert [r["summary"] for r in storage.get_summaries("c1")] == ["first", "second"]
    assert storage.get_summaries("other") == []
