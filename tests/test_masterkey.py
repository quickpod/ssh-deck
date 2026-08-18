r"""The master password: enabling it, unlocking, and the no-recovery promise."""

from __future__ import annotations

import json
import os

import pytest

from sshdeck import masterkey, sessions, vault
from sshdeck.masterkey import Locked
from sshdeck.vault import WrongPassphrase

pytestmark = pytest.mark.skipif(not vault.crypto_available(),
                                reason="cryptography not installed")

PW = "a master password"
ITEMS = [{"name": "web", "host": "a.example.com", "port": 22, "user": "deploy",
          "auth": "key", "key_path": None, "jump": None}]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(masterkey, "config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sessions, "config_dir", lambda: str(tmp_path))
    masterkey.lock()
    yield tmp_path
    masterkey.lock()


# --------------------------------------------------------------------------- #
# Turning it on
# --------------------------------------------------------------------------- #
def test_profile_starts_unprotected(home):
    assert masterkey.is_configured() is False


def test_enable_creates_the_vault_and_unlocks_it(home):
    masterkey.enable(PW, ITEMS, "the usual")
    assert masterkey.is_configured() is True
    assert masterkey.is_unlocked() is True
    assert masterkey.read() == ITEMS


def test_enable_moves_the_plaintext_aside(home):
    plain = home / "sessions.json"
    plain.write_text(json.dumps(ITEMS), encoding="utf-8")
    masterkey.enable(PW, ITEMS)
    assert not plain.exists()
    assert (home / "sessions.json.pre-vault").exists()


def test_enable_refuses_an_empty_passphrase(home):
    with pytest.raises(vault.VaultError):
        masterkey.enable("", ITEMS)


def test_the_vault_holds_no_readable_session_data(home):
    masterkey.enable(PW, ITEMS)
    blob = (home / "sessions.vault").read_text(encoding="utf-8")
    assert "a.example.com" not in blob and "deploy" not in blob


# --------------------------------------------------------------------------- #
# Unlocking
# --------------------------------------------------------------------------- #
def test_reading_while_locked_raises_rather_than_returning_nothing(home):
    """Returning [] would look like "no sessions" and invite overwriting them."""
    masterkey.enable(PW, ITEMS)
    masterkey.lock()
    with pytest.raises(Locked):
        masterkey.read()


def test_writing_while_locked_is_refused(home):
    masterkey.enable(PW, ITEMS)
    masterkey.lock()
    with pytest.raises(Locked):
        masterkey.write(ITEMS)


def test_unlock_with_the_right_password(home):
    masterkey.enable(PW, ITEMS)
    masterkey.lock()
    assert masterkey.unlock(PW) == ITEMS
    assert masterkey.is_unlocked() is True


def test_unlock_with_the_wrong_password_is_refused(home):
    masterkey.enable(PW, ITEMS)
    masterkey.lock()
    with pytest.raises(WrongPassphrase):
        masterkey.unlock("not it")
    assert masterkey.is_unlocked() is False


def test_lock_drops_the_passphrase(home):
    masterkey.enable(PW, ITEMS)
    masterkey.lock()
    assert masterkey.is_unlocked() is False


# --------------------------------------------------------------------------- #
# Hint — the only concession to a forgotten password
# --------------------------------------------------------------------------- #
def test_hint_is_readable_while_locked(home):
    masterkey.enable(PW, ITEMS, "same as the laptop")
    masterkey.lock()
    assert masterkey.hint() == "same as the laptop"


def test_no_hint_is_empty(home):
    masterkey.enable(PW, ITEMS)
    assert masterkey.hint() == ""


def test_there_is_no_recovery_path(home):
    """A forgotten password means the data is gone -- assert we offer nothing."""
    masterkey.enable(PW, ITEMS, "a hint")
    masterkey.lock()
    exported = {n for n in dir(masterkey) if not n.startswith("_")}
    assert not exported & {"recover", "reset", "bypass", "master_key",
                           "backdoor", "export_key"}
    with pytest.raises(WrongPassphrase):
        masterkey.unlock("guess")


# --------------------------------------------------------------------------- #
# Changing and turning it off
# --------------------------------------------------------------------------- #
def test_change_password_keeps_the_sessions_and_hint(home):
    masterkey.enable(PW, ITEMS, "keep me")
    masterkey.change(PW, "second password")
    masterkey.lock()
    assert masterkey.unlock("second password") == ITEMS
    assert masterkey.hint() == "keep me"


def test_change_requires_the_old_password(home):
    masterkey.enable(PW, ITEMS)
    with pytest.raises(WrongPassphrase):
        masterkey.change("wrong", "new")


def test_disable_returns_the_sessions_and_removes_the_vault(home):
    masterkey.enable(PW, ITEMS)
    got = masterkey.disable(PW)
    assert got == ITEMS
    assert masterkey.is_configured() is False
    assert masterkey.is_unlocked() is False


# --------------------------------------------------------------------------- #
# The session store goes through it transparently
# --------------------------------------------------------------------------- #
def test_sessions_round_trip_through_the_vault(home):
    masterkey.enable(PW, ITEMS)
    loaded = sessions.load_all()
    assert [s.name for s in loaded] == ["web"]
    loaded.append(sessions.Session(name="db", host="b.example.com"))
    sessions.save_all(loaded)
    masterkey.lock(); masterkey.unlock(PW)
    assert {s.name for s in sessions.load_all()} == {"web", "db"}


def test_an_explicit_path_still_uses_plaintext(home, tmp_path):
    """Tests and imports that name a file must not be hijacked by the vault."""
    masterkey.enable(PW, ITEMS)
    other = str(tmp_path / "elsewhere.json")
    sessions.save_all([sessions.Session(name="x", host="c.example.com")],
                      path=other)
    assert os.path.exists(other)
    assert [s.name for s in sessions.load_all(path=other)] == ["x"]
