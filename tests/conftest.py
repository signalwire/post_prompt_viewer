import pytest


@pytest.fixture(autouse=True)
def _tmp_env(tmp_path, monkeypatch):
    """Give every test a fresh data dir and disable background analysis."""
    monkeypatch.setenv("PPV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PPV_AUTO_ANALYZE", "false")
    from post_prompt_viewer.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
