import base64
import json

from fastapi.testclient import TestClient


def _client(monkeypatch, user="u", pw="p"):
    monkeypatch.setenv("PPV_AUTH_USER", user)
    monkeypatch.setenv("PPV_AUTH_PASS", pw)
    from post_prompt_viewer.config import get_settings

    get_settings.cache_clear()
    from post_prompt_viewer.app import create_app

    return TestClient(create_app())


def _basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_blocks_without_credentials(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/")
    assert r.status_code == 401
    assert "Basic" in r.headers.get("WWW-Authenticate", "")


def test_allows_with_credentials(monkeypatch):
    c = _client(monkeypatch)
    assert c.get("/", headers={"Authorization": _basic("u", "p")}).status_code == 200
    assert c.get("/", headers={"Authorization": _basic("u", "nope")}).status_code == 401


def test_ingest_and_health_exempt(monkeypatch):
    c = _client(monkeypatch)
    # The agent must still post without credentials.
    body = json.dumps({"action": "fetch_conversation", "conversation_id": "x"})
    assert c.post("/collect", content=body).status_code == 200
    assert c.post("/", content=body).status_code == 200
    assert c.get("/api/health").status_code == 200


def test_collect_auth_is_separate(monkeypatch):
    monkeypatch.setenv("PPV_COLLECT_USER", "c")
    monkeypatch.setenv("PPV_COLLECT_PASS", "s")
    from post_prompt_viewer.config import get_settings

    get_settings.cache_clear()
    from post_prompt_viewer.app import create_app

    client = TestClient(create_app())
    body = json.dumps({"action": "fetch_conversation", "conversation_id": "x"})
    assert client.post("/collect", content=body).status_code == 401          # no creds
    assert client.post("/", content=body).status_code == 401                 # alias gated too
    assert client.post("/collect", content=body,
                       headers={"Authorization": _basic("c", "s")}).status_code == 200
    assert client.post("/collect", content=body,
                       headers={"Authorization": _basic("c", "bad")}).status_code == 401
    assert client.get("/").status_code == 200                                # viewer still open
    get_settings.cache_clear()


def test_disabled_when_unset(monkeypatch):
    # No PPV_AUTH_* set (the autouse fixture doesn't set them): viewer is open.
    from post_prompt_viewer.config import get_settings

    get_settings.cache_clear()
    from post_prompt_viewer.app import create_app

    assert TestClient(create_app()).get("/").status_code == 200
