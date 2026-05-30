import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from post_prompt_viewer.app import create_app

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "samples"
    / "conversation_demo.json"
)


@pytest.fixture
def client():
    return TestClient(create_app())


def test_collect_and_views(client):
    payload = json.loads(SAMPLE.read_text())
    assert client.post("/collect", content=json.dumps(payload)).json() == {"response": "data received"}
    assert client.get("/api/health").json()["calls"] == 1
    cid = payload["call_id"]
    assert client.get("/").status_code == 200
    assert client.get(f"/c/{cid}").status_code == 200
    assert client.get(f"/api/call/{cid}").json()["app_name"] == "demo agent"
    assert client.get("/c/does-not-exist").status_code == 404


def test_conversation_memory_dropin(client):
    # Empty lookup
    miss = client.post("/collect", content=json.dumps(
        {"action": "fetch_conversation", "conversation_id": "cust-1"})).json()
    assert miss["response"].startswith("No previous")
    # Save then recall
    client.post("/collect", content=json.dumps(
        {"conversation_id": "cust-1", "conversation_summary": "wanted to top up balance"}))
    hit = client.post("/collect", content=json.dumps(
        {"action": "fetch_conversation", "conversation_id": "cust-1"})).json()
    assert hit["response"] == "Conversation found"
    assert "wanted to top up balance" in hit["conversation_summary"]


def test_upload_endpoint(client):
    payload = json.loads(SAMPLE.read_text())
    payload["call_id"] = "uploaded-1"
    r = client.post("/upload", content=json.dumps(payload))
    assert r.status_code == 200
    body = r.json()
    assert body["call_id"] == "uploaded-1"
    assert body["url"].endswith("/c/uploaded-1")
    assert client.get("/c/uploaded-1").status_code == 200
    # rejects non-payload JSON and non-JSON
    assert client.post("/upload", content='{"foo": 1}').status_code == 400
    assert client.post("/upload", content="not json at all").status_code == 400


def test_collect_dead_letters_bad_payloads(client):
    from post_prompt_viewer.config import get_settings

    rd = get_settings().rejected_dir
    before = len(list(rd.glob("*.json")))
    # unparseable + valid-JSON-but-not-a-payload are both preserved, not dropped
    client.post("/collect", content="not json {{")
    client.post("/collect", content='{"foo": 1}', headers={"content-type": "application/json"})
    assert len(list(rd.glob("*.json"))) == before + 2
    # a real payload is stored, not dead-lettered
    client.post("/collect", content=json.dumps({"call_id": "ok1", "call_log": []}))
    assert len(list(rd.glob("*.json"))) == before + 2
    assert client.get("/c/ok1").status_code == 200


def test_cgi_form_postdata_fallback(client):
    payload = {"conversation_id": "cust-2", "conversation_summary": "via form POSTDATA"}
    client.post("/collect", data={"POSTDATA": json.dumps(payload)})
    hit = client.post("/collect", content=json.dumps(
        {"action": "fetch_conversation", "conversation_id": "cust-2"})).json()
    assert "via form POSTDATA" in hit["conversation_summary"]
