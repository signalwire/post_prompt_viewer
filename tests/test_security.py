import json

import pytest
from fastapi.testclient import TestClient

from post_prompt_viewer import enrich, recordings, storage
from post_prompt_viewer.app import create_app
from post_prompt_viewer.config import get_settings


def test_safe_id_strips_traversal():
    assert enrich.safe_id("../../etc/passwd") == "etcpasswd"
    assert enrich.safe_id("a/b\\c") == "abc"
    assert enrich.safe_id("..hidden") == "hidden"
    assert enrich.safe_id("good-id_1.2") == "good-id_1.2"


def test_derive_index_sanitizes_call_id():
    idx = enrich.derive_index({"call_id": "../../evil", "call_log": []}, received_at_us=1)
    assert "/" not in idx["call_id"] and ".." not in idx["call_id"]


def test_fetch_guard_blocks_ssrf_and_lfi():
    get_settings.cache_clear()
    s = get_settings()
    # file:// and local paths are off by default
    for bad in ("file:///etc/passwd", "/etc/passwd", "gopher://x/", "ftp://x/y"):
        with pytest.raises(ValueError):
            recordings._assert_fetchable(bad, s)
    # private / loopback / link-local (cloud metadata) hosts are refused
    for host in ("127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1"):
        with pytest.raises(ValueError):
            recordings._assert_fetchable("http://" + host + "/x.mp4", s)


def test_fetch_guard_allows_local_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PPV_ALLOW_LOCAL_RECORDINGS", "1")
    get_settings.cache_clear()
    s = get_settings()
    f = tmp_path / "rec.wav"
    f.write_bytes(b"x")
    recordings._assert_fetchable("file://" + str(f), s)  # no raise
    get_settings.cache_clear()


def test_host_allowlist(monkeypatch):
    monkeypatch.setenv("PPV_RECORDING_HOST_ALLOWLIST", "files.signalwire.com")
    get_settings.cache_clear()
    s = get_settings()
    with pytest.raises(ValueError):
        recordings._assert_fetchable("https://evil.example.com/x.mp4", s)
    get_settings.cache_clear()


def test_audio_endpoint_rejects_path_escape():
    client = TestClient(create_app())
    storage.init_db()
    storage.save_call({"call_id": "x1", "call_log": []})
    storage.set_recording("x1", status="done", audio_path="/etc/passwd")
    assert client.get("/api/recording/x1/audio").status_code == 404


def test_transcript_is_html_escaped():
    client = TestClient(create_app())
    storage.init_db()
    storage.save_call({"call_id": "x2", "call_log": [
        {"role": "user", "content": "<script>alert(1)</script>", "timestamp": 1}]})
    html = client.get("/c/x2").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_ingest_body_size_cap(monkeypatch):
    monkeypatch.setenv("PPV_MAX_INGEST_BYTES", "10")
    get_settings.cache_clear()
    client = TestClient(create_app())
    r = client.post("/collect", content=json.dumps({"call_id": "z", "call_log": []}))
    assert r.status_code == 413
    get_settings.cache_clear()
