"""Session CRUD + the no-plaintext-secret guarantee (no server needed)."""

import json

import pytest

from sshdeck import sessions
from sshdeck.errors import SSHDeckError


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "sessions.json")


def test_add_get_update_remove(store):
    s = sessions.Session("web", "example.com", port=2222, user="deploy",
                         auth="key", key_path="/home/me/.ssh/id_ed25519")
    sessions.add(s, path=store)
    assert [x.name for x in sessions.load_all(store)] == ["web"]

    got = sessions.get("web", path=store)
    assert got.host == "example.com"
    assert got.port == 2222
    assert got.user == "deploy"

    got2 = sessions.Session("web", "example.com", port=22, user="root")
    sessions.update(got2, path=store)
    assert sessions.get("web", path=store).port == 22

    sessions.remove("web", path=store)
    assert sessions.load_all(store) == []


def test_add_duplicate_name_raises(store):
    sessions.add(sessions.Session("a", "h1"), path=store)
    with pytest.raises(SSHDeckError):
        sessions.add(sessions.Session("a", "h2"), path=store)


def test_upsert_replaces(store):
    sessions.upsert(sessions.Session("a", "h1"), path=store)
    sessions.upsert(sessions.Session("a", "h2"), path=store)
    all_ = sessions.load_all(store)
    assert len(all_) == 1 and all_[0].host == "h2"


def test_get_missing_raises(store):
    with pytest.raises(SSHDeckError):
        sessions.get("nope", path=store)


def test_remove_missing_raises(store):
    with pytest.raises(SSHDeckError):
        sessions.remove("nope", path=store)


@pytest.mark.parametrize("bad", [
    dict(name="", host="h"),          # empty name
    dict(name="n", host=""),          # empty host
    dict(name="n", host="h", port=0),         # bad port
    dict(name="n", host="h", port=70000),     # bad port
    dict(name="n", host="h", port="abc"),     # non-numeric port
    dict(name="n", host="h", auth="magic"),   # bad auth
])
def test_validation(bad):
    with pytest.raises(SSHDeckError):
        sessions.Session(**bad)


def test_no_plaintext_password_ever_persisted(store):
    """Even if a caller sneaks a password onto the object/dict, disk stays clean."""
    s = sessions.Session("secure", "host", user="me", auth="password")
    # Simulate an attribute being forced on (should not be serialized).
    with pytest.raises((AttributeError, TypeError)):
        s.password = "hunter2"  # __slots__ forbids unknown attributes

    sessions.add(s, path=store)
    raw = open(store, "r", encoding="utf-8").read()
    # No secret VALUES on disk (auth="password" is a legitimate method name, so
    # we check that no entry carries a password/passphrase KEY, and no secret).
    for entry in json.loads(raw):
        assert "password" not in entry
        assert "passphrase" not in entry
        assert "secret" not in entry
    assert "hunter2" not in raw

    # from_dict must drop any secret keys defensively.
    d = {"name": "x", "host": "h", "password": "leak", "passphrase": "leak2"}
    got = sessions.Session.from_dict(d)
    assert "password" not in got.to_dict()
    assert "leak" not in json.dumps(got.to_dict())


def test_from_dict_roundtrip():
    s = sessions.Session("n", "h", port=2200, user="u", auth="agent", jump="b@x:22")
    assert sessions.Session.from_dict(s.to_dict()) == s
