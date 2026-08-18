r"""The global identity key, and its passphrase for the life of one run.

SecureCRT keeps a single "global public key" that every new session inherits
unless it overrides it, and asks for that key's passphrase once per run rather
than per connection.  This module is the same idea:

* :func:`global_key_path` is the key new sessions inherit; a session's own
  ``key_path`` always wins when it sets one.
* :func:`unlock` decrypts a key **once** and keeps the decrypted key object in
  memory for the rest of the run, so the second and subsequent connections do
  not re-prompt.

**The passphrase is never written anywhere** -- not to the config, not to the
vault, not to a temp file.  Only the decrypted key object lives in memory, and
only until :func:`forget_all` or process exit.  That is a deliberate contrast
with saved *passwords*, which the user may opt into storing in the encrypted
vault: a key passphrase protects a file the user already holds, so caching it
in RAM for one run is the right trade, while persisting it is not.

paramiko is imported lazily so this module stays importable (and testable)
without it.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from .errors import SSHDeckError
from . import guiconfig

#: Decrypted key objects for this run, keyed by absolute path. Never persisted.
_unlocked: Dict[str, object] = {}
_lock = threading.RLock()


class KeyError_(SSHDeckError):
    """Raised when a private key cannot be read or decrypted."""


class PassphraseRequired(KeyError_):
    """The key is encrypted and no (or a wrong) passphrase was supplied."""


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path or "").strip()))


def global_key_path() -> Optional[str]:
    """The configured global identity key, or None when unset."""
    path = (guiconfig.get_global_key() or "").strip()
    return _abs(path) if path else None


def set_global_key_path(path: Optional[str]) -> None:
    guiconfig.set_global_key(_abs(path) if path else "")


def effective_key_path(session) -> Optional[str]:
    """The key *session* should use: its own, else the global one.

    Mirrors SecureCRT's "Use global public key" checkbox without needing a
    per-session flag -- an empty ``key_path`` simply means "inherit".
    """
    own = getattr(session, "key_path", None)
    if own:
        return _abs(own)
    return global_key_path()


def _load(path: str, passphrase: Optional[str]):
    """Load a private key of any supported type, or raise."""
    try:
        import paramiko
    except Exception as exc:  # pragma: no cover - paramiko is a hard dep
        raise KeyError_(f"paramiko is required to read private keys ({exc}).")

    if not os.path.isfile(path):
        raise KeyError_(f"No private key at {path}.")

    last: Optional[Exception] = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey,
                getattr(paramiko, "DSSKey", None)):
        if cls is None:
            continue
        try:
            return cls.from_private_key_file(path, password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise PassphraseRequired(
                f"{os.path.basename(path)} is protected by a passphrase.")
        except paramiko.SSHException as exc:
            last = exc          # wrong type for this class, or bad passphrase
        except OSError as exc:
            raise KeyError_(f"Could not read {path}: {exc}.")
    # Every type failed. With a passphrase supplied the likeliest cause by far
    # is that it was wrong, so say that rather than "unsupported key format".
    if passphrase:
        raise PassphraseRequired(
            f"That passphrase did not unlock {os.path.basename(path)}.")
    raise KeyError_(f"{os.path.basename(path)} is not a supported private key "
                    f"({last}).")


def is_encrypted(path: str) -> bool:
    """True when *path* needs a passphrase.  Never raises for a missing file."""
    path = _abs(path)
    if not os.path.isfile(path):
        return False
    try:
        _load(path, None)
        return False
    except PassphraseRequired:
        return True
    except KeyError_:
        return False


def is_unlocked(path: str) -> bool:
    with _lock:
        return _abs(path) in _unlocked


def unlock(path: str, passphrase: Optional[str] = None):
    """Return the decrypted key for *path*, caching it for this run.

    Call with no passphrase first: an unencrypted key (or one already unlocked)
    succeeds immediately, and only a :class:`PassphraseRequired` means the
    caller has to prompt.  That is what lets one prompt serve every session.
    """
    key_path = _abs(path)
    with _lock:
        cached = _unlocked.get(key_path)
    if cached is not None:
        return cached
    key = _load(key_path, passphrase)
    with _lock:
        _unlocked[key_path] = key
    return key


def forget(path: str) -> None:
    """Drop one cached key (e.g. the user re-pointed the session elsewhere)."""
    with _lock:
        _unlocked.pop(_abs(path), None)


def forget_all() -> None:
    """Drop every cached key -- call when locking the app or signing out."""
    with _lock:
        _unlocked.clear()


def unlocked_count() -> int:
    """How many keys are currently held (for status display and tests)."""
    with _lock:
        return len(_unlocked)
