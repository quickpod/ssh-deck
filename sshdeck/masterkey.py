r"""The master password: one passphrase that protects the whole profile.

Session profiles live in ``sessions.json`` in the clear until a master
password is set. Once it is, they move into ``sessions.vault`` -- a single
encrypted envelope (see :mod:`sshdeck.vault`) -- and the plaintext file is
removed. Nothing else in the app changes: :mod:`sshdeck.sessions` reads and
writes through here either way.

**There is no recovery.** The passphrase is never stored, so a forgotten one
means the vault cannot be opened by anybody, including us. That is the
property that makes it worth having, and the reason the setup screen says so
before accepting one. A *hint* can be saved to soften it: it sits outside the
ciphertext so someone locked out can still read it, which also means it must
not paraphrase the passphrase itself.

The decrypted contents are held in memory for the life of the app and dropped
on :func:`lock`.
"""

from __future__ import annotations

import os
import threading
from typing import Any, List, Optional

from . import vault
from .errors import SSHDeckError
from .guiconfig import config_dir

VAULT_NAME = "sessions.vault"
PLAIN_NAME = "sessions.json"

_lock = threading.RLock()
_passphrase: Optional[str] = None      # held only while unlocked
_cache: Optional[List[Any]] = None


class Locked(SSHDeckError):
    """Raised when the vault exists but has not been unlocked this run."""


def vault_path(path: Optional[str] = None) -> str:
    return path or os.path.join(config_dir(), VAULT_NAME)


def plain_path(path: Optional[str] = None) -> str:
    return path or os.path.join(config_dir(), PLAIN_NAME)


def is_configured(path: Optional[str] = None) -> bool:
    """True when a master password protects this profile."""
    return os.path.exists(vault_path(path))


def is_unlocked() -> bool:
    with _lock:
        return _passphrase is not None


def hint(path: Optional[str] = None) -> str:
    """The saved reminder, readable without unlocking."""
    return vault.read_hint(vault_path(path))


def unlock(passphrase: str, path: Optional[str] = None) -> List[Any]:
    """Open the vault and hold it for this run. Raises on a wrong passphrase."""
    data = vault.read_vault(vault_path(path), passphrase)
    if not isinstance(data, list):
        raise vault.VaultError("The vault opened but its contents are damaged.")
    with _lock:
        global _passphrase, _cache
        _passphrase = passphrase
        _cache = data
    return data


def lock() -> None:
    """Drop the decrypted contents and the passphrase."""
    with _lock:
        global _passphrase, _cache
        _passphrase = None
        _cache = None


def read(path: Optional[str] = None) -> List[Any]:
    """The session list from the vault. Requires an unlock first."""
    with _lock:
        if _passphrase is None:
            raise Locked("The profile is locked — enter the master password.")
        return list(_cache or [])


def write(items: List[Any], path: Optional[str] = None) -> None:
    """Re-seal the session list under the current passphrase."""
    with _lock:
        if _passphrase is None:
            raise Locked("The profile is locked — enter the master password.")
        vault.write_vault(vault_path(path), items, _passphrase,
                          hint(path))
        global _cache
        _cache = list(items)


def enable(passphrase: str, items: List[Any], hint_text: str = "",
           path: Optional[str] = None) -> None:
    """Turn on the master password, moving *items* into the vault.

    The plaintext file is removed only after the vault has been written and
    read back successfully -- a failure part way through must not leave the
    user with neither copy.
    """
    if not passphrase:
        raise vault.VaultError("Choose a master password.")
    target = vault_path(path)
    vault.write_vault(target, items, passphrase, hint_text)
    check = vault.read_vault(target, passphrase)      # prove it opens
    if check != items:
        raise vault.VaultError("The vault did not read back correctly.")
    with _lock:
        global _passphrase, _cache
        _passphrase = passphrase
        _cache = list(items)
    plain = plain_path(path)
    if os.path.exists(plain):
        try:
            os.replace(plain, plain + ".pre-vault")
        except OSError:
            pass          # keeping the old copy is better than failing here


def disable(passphrase: str, path: Optional[str] = None) -> List[Any]:
    """Turn the master password off, returning the sessions to plaintext."""
    items = vault.read_vault(vault_path(path), passphrase)
    lock()
    try:
        os.unlink(vault_path(path))
    except OSError as exc:
        raise vault.VaultError(f"Could not remove the vault: {exc}.")
    return items if isinstance(items, list) else []


def change(old: str, new: str, hint_text: Optional[str] = None,
           path: Optional[str] = None) -> None:
    """Change the master password, keeping the hint unless a new one is given."""
    vault.change_passphrase(vault_path(path), old, new, hint_text)
    with _lock:
        global _passphrase
        if _passphrase is not None:
            _passphrase = new
