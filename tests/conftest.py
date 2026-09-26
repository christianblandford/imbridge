import pytest

from imbridge import config


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests away from the real ~/Library/Application Support/imbridge (allowlist, send counts, token)."""
    monkeypatch.setattr(config, "APP_SUPPORT", tmp_path / "state")
