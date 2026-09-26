import pytest
from helpers import make_chat_db

from imbridge import config


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests away from the real ~/Library/Application Support/imbridge (allowlist, send counts, token) and from
    Messages' own folders."""
    monkeypatch.setattr(config, "APP_SUPPORT", tmp_path / "state")
    monkeypatch.setattr(config, "OUTGOING", tmp_path / "outgoing")


@pytest.fixture
def chat_db(tmp_path):
    return make_chat_db(tmp_path / "chat.db")
