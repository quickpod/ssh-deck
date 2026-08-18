r"""Global identity key + one-prompt-per-run passphrase caching."""

from __future__ import annotations

import os

import pytest

from sshdeck import identity, guiconfig
from sshdeck.identity import PassphraseRequired, KeyError_
from sshdeck.sessions import Session

paramiko = pytest.importorskip("paramiko")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDECK_HOME", str(tmp_path))
    monkeypatch.setattr(guiconfig, "config_dir", lambda: str(tmp_path))
    identity.forget_all()
    yield
    identity.forget_all()


def _write_key(path, passphrase=None):
    key = paramiko.Ed25519Key.generate() if hasattr(paramiko.Ed25519Key, "generate") \
        else paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(path), password=passphrase)
    return str(path)


# --------------------------------------------------------------------------- #
# Inheritance: a session's own key wins, else the global one
# --------------------------------------------------------------------------- #
def test_session_without_a_key_inherits_the_global_one(tmp_path):
    identity.set_global_key_path(str(tmp_path / "global.pem"))
    s = Session(name="a", host="h")
    assert identity.effective_key_path(s) == str(tmp_path / "global.pem")


def test_session_key_overrides_the_global_one(tmp_path):
    identity.set_global_key_path(str(tmp_path / "global.pem"))
    s = Session(name="a", host="h", key_path=str(tmp_path / "own.pem"))
    assert identity.effective_key_path(s) == str(tmp_path / "own.pem")


def test_no_global_key_means_none():
    identity.set_global_key_path("")
    assert identity.effective_key_path(Session(name="a", host="h")) is None


def test_global_key_persists_across_loads(tmp_path):
    identity.set_global_key_path(str(tmp_path / "k.pem"))
    assert guiconfig.get_global_key().endswith("k.pem")


# --------------------------------------------------------------------------- #
# Passphrase handling
# --------------------------------------------------------------------------- #
def test_unencrypted_key_unlocks_without_a_passphrase(tmp_path):
    p = _write_key(tmp_path / "plain.pem")
    assert identity.is_encrypted(p) is False
    assert identity.unlock(p) is not None


def test_encrypted_key_is_detected_and_demands_a_passphrase(tmp_path):
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    assert identity.is_encrypted(p) is True
    with pytest.raises(PassphraseRequired):
        identity.unlock(p)


def test_correct_passphrase_unlocks(tmp_path):
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    assert identity.unlock(p, "s3cret") is not None


def test_wrong_passphrase_is_reported_as_such(tmp_path):
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    with pytest.raises(PassphraseRequired):
        identity.unlock(p, "nope")


def test_failed_unlock_caches_nothing(tmp_path):
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    with pytest.raises(PassphraseRequired):
        identity.unlock(p, "nope")
    assert identity.unlocked_count() == 0
    assert identity.is_unlocked(p) is False


# --------------------------------------------------------------------------- #
# Ask once, reuse for the rest of the run
# --------------------------------------------------------------------------- #
def test_second_unlock_needs_no_passphrase(tmp_path):
    """The point of the cache: one prompt serves every later session."""
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    first = identity.unlock(p, "s3cret")
    again = identity.unlock(p)           # no passphrase this time
    assert again is first
    assert identity.is_unlocked(p) is True


def test_forget_all_drops_the_cache(tmp_path):
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    identity.unlock(p, "s3cret")
    identity.forget_all()
    assert identity.unlocked_count() == 0
    with pytest.raises(PassphraseRequired):
        identity.unlock(p)               # prompts again after locking


def test_forget_one_key_leaves_others(tmp_path):
    a = _write_key(tmp_path / "a.pem")
    b = _write_key(tmp_path / "b.pem")
    identity.unlock(a); identity.unlock(b)
    identity.forget(a)
    assert identity.is_unlocked(a) is False
    assert identity.is_unlocked(b) is True


def test_the_passphrase_itself_is_never_stored(tmp_path):
    """Only the decrypted key object is held -- never the passphrase."""
    p = _write_key(tmp_path / "enc.pem", passphrase="s3cret")
    identity.unlock(p, "s3cret")
    assert "s3cret" not in repr(identity._unlocked)
    cfg = os.path.join(str(tmp_path), "config.json")
    if os.path.exists(cfg):
        assert "s3cret" not in open(cfg).read()


# --------------------------------------------------------------------------- #
# Bad input
# --------------------------------------------------------------------------- #
def test_missing_key_file_reports_clearly(tmp_path):
    with pytest.raises(KeyError_):
        identity.unlock(str(tmp_path / "nope.pem"))


def test_is_encrypted_is_false_for_a_missing_file(tmp_path):
    assert identity.is_encrypted(str(tmp_path / "nope.pem")) is False


def test_garbage_file_is_not_a_key(tmp_path):
    p = tmp_path / "junk.pem"
    p.write_text("not a key at all", encoding="utf-8")
    assert identity.is_encrypted(str(p)) is False
    with pytest.raises(KeyError_):
        identity.unlock(str(p))
