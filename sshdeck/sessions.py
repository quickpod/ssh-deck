r"""Session profiles for SSHDeck -- saved connection settings, persisted as JSON.

A :class:`Session` records everything needed to *reach* a host: name, host, port,
username, the authentication method, an optional private-key path and an optional
jump host.  It deliberately records **no secret**: passwords and key passphrases
are never stored on disk -- they are prompted for at connect time.  Even if a dict
handed to :meth:`Session.from_dict` carries a ``password``/``passphrase`` key it is
dropped, and :meth:`Session.to_dict` can never emit one.

The store is a single JSON file (a list of profiles) at
``config_dir()/sessions.json``; CRUD helpers accept an explicit ``path`` so tests
can point at a scratch file.
"""

from __future__ import annotations

import json
import os

from .errors import SSHDeckError
from .guiconfig import config_dir

STORE_NAME = "sessions.json"

AUTH_METHODS = ("key", "password", "agent")

# Keys that must NEVER be persisted, whatever the caller passes in.
_SECRET_KEYS = ("password", "passphrase", "secret", "pass")


def store_path(path=None):
    """Absolute path to the sessions store (``config_dir()/sessions.json``)."""
    return path or os.path.join(config_dir(), STORE_NAME)


class Session:
    """A single saved connection profile (host/user/auth -- never a secret)."""

    __slots__ = ("name", "host", "port", "user", "auth", "key_path", "jump")

    def __init__(self, name, host, port=22, user=None, auth="key",
                 key_path=None, jump=None):
        self.name = _clean_name(name)
        self.host = _clean_host(host)
        self.port = _clean_port(port)
        self.user = (user or "").strip() or None
        self.auth = _clean_auth(auth)
        self.key_path = (key_path or "").strip() or None
        # jump is another host spec: "user@host:port" or "" -- stored as a string
        self.jump = (jump or "").strip() or None

    # -- serialization ---------------------------------------------------
    def to_dict(self):
        """A JSON-safe dict.  Guaranteed to contain no secret material."""
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "auth": self.auth,
            "key_path": self.key_path,
            "jump": self.jump,
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SSHDeckError("session entry is not an object")
        # Drop any secret-ish keys defensively before constructing.
        clean = {k: v for k, v in data.items() if k.lower() not in _SECRET_KEYS}
        try:
            return cls(
                name=clean.get("name"),
                host=clean.get("host"),
                port=clean.get("port", 22),
                user=clean.get("user"),
                auth=clean.get("auth", "key"),
                key_path=clean.get("key_path"),
                jump=clean.get("jump"),
            )
        except SSHDeckError:
            raise
        except Exception as exc:
            raise SSHDeckError(f"invalid session entry: {exc}")

    def target(self):
        """A friendly ``user@host:port`` description."""
        who = f"{self.user}@" if self.user else ""
        return f"{who}{self.host}:{self.port}"

    def __repr__(self):
        return f"Session({self.name!r} -> {self.target()})"

    def __eq__(self, other):
        return isinstance(other, Session) and self.to_dict() == other.to_dict()


# -- validation helpers ------------------------------------------------------
def _clean_name(name):
    name = (name or "").strip()
    if not name:
        raise SSHDeckError("session name is required")
    return name


def _clean_host(host):
    host = (host or "").strip()
    if not host:
        raise SSHDeckError("host is required")
    return host


def _clean_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise SSHDeckError(f"port must be a whole number, got {port!r}")
    if not (1 <= port <= 65535):
        raise SSHDeckError(f"port must be between 1 and 65535, got {port}")
    return port


def _clean_auth(auth):
    auth = (auth or "key").strip().lower()
    if auth not in AUTH_METHODS:
        raise SSHDeckError(
            f"auth must be one of {', '.join(AUTH_METHODS)}, got {auth!r}")
    return auth


# -- store CRUD --------------------------------------------------------------
def load_all(path=None):
    """Return the list of :class:`Session` from the store (``[]`` if none).

    A missing store is empty; a corrupt store raises :class:`SSHDeckError` so the
    user is told rather than silently losing every saved profile.
    """
    p = store_path(path)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise SSHDeckError(f"could not read sessions from {p}: {exc}")
    if not isinstance(data, list):
        raise SSHDeckError(f"sessions file {p} is malformed (expected a list)")
    return [Session.from_dict(d) for d in data]


def save_all(sessions, path=None):
    """Write *sessions* (an iterable of :class:`Session`) to the store."""
    p = store_path(path)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        payload = [s.to_dict() for s in sessions]
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, p)
    except SSHDeckError:
        raise
    except Exception as exc:
        raise SSHDeckError(f"could not write sessions to {p}: {exc}")


def get(name, path=None):
    """Return the session named *name* or raise :class:`SSHDeckError`."""
    for s in load_all(path):
        if s.name == name:
            return s
    raise SSHDeckError(f"no session named {name!r}")


def exists(name, path=None):
    return any(s.name == name for s in load_all(path))


def add(session, path=None):
    """Add a new profile.  Raises if a profile with that name already exists."""
    sessions = load_all(path)
    if any(s.name == session.name for s in sessions):
        raise SSHDeckError(
            f"a session named {session.name!r} already exists (use update)")
    sessions.append(session)
    save_all(sessions, path)
    return session


def update(session, path=None):
    """Replace an existing profile by name (raises if it is not present)."""
    sessions = load_all(path)
    for i, s in enumerate(sessions):
        if s.name == session.name:
            sessions[i] = session
            save_all(sessions, path)
            return session
    raise SSHDeckError(f"no session named {session.name!r} to update")


def upsert(session, path=None):
    """Add *session*, or replace an existing one with the same name."""
    sessions = load_all(path)
    for i, s in enumerate(sessions):
        if s.name == session.name:
            sessions[i] = session
            save_all(sessions, path)
            return session
    sessions.append(session)
    save_all(sessions, path)
    return session


def remove(name, path=None):
    """Delete the profile named *name* (raises if it is not present)."""
    sessions = load_all(path)
    kept = [s for s in sessions if s.name != name]
    if len(kept) == len(sessions):
        raise SSHDeckError(f"no session named {name!r} to remove")
    save_all(kept, path)
    return True
