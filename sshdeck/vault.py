r"""Encrypted store for session profiles and their credentials.

SSHDeck historically wrote **no** secret to disk and prompted for every
password at connect time.  That is still what happens until the user sets a
master passphrase: this module is entirely opt-in, and with no passphrase set
the store stays plaintext-but-secretless exactly as before.

Once a passphrase *is* set, profiles and credentials are sealed into a single
encrypted envelope (SecureCRT calls the equivalent a "Configuration
Passphrase").  The design rules:

* **The passphrase is never stored** -- not on disk, not in the envelope.  A
  wrong one is indistinguishable from a corrupt file, which is the point: the
  AEAD tag is the only verifier, so there is nothing offline-crackable beyond
  the KDF itself.
* **Argon2id** derives the key where available (memory-hard, so a stolen file
  resists GPU cracking), falling back to scrypt on older builds.  The
  parameters live *in* the envelope, so raising them later does not strand
  existing files.
* **AES-256-GCM** seals the payload.  The envelope header is authenticated as
  associated data, so an attacker cannot downgrade the KDF or swap the salt
  without the tag failing.
* **The derived key stays in memory only** for the unlocked session.  Locking
  drops it; the file on disk is never rewritten in the clear.

Nothing here touches the network or the GUI, so it is fully testable.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any, Dict, Optional

from .errors import SSHDeckError

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO = True
except Exception:  # pragma: no cover - cryptography ships with paramiko
    AESGCM = None
    _CRYPTO = False

try:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    _ARGON2 = True
except Exception:  # pragma: no cover - older cryptography
    Argon2id = None
    _ARGON2 = False

try:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except Exception:  # pragma: no cover
    Scrypt = None


MAGIC = "SSHDeckVault"
VERSION = 1
KEY_BYTES = 32          # AES-256
SALT_BYTES = 16
NONCE_BYTES = 12        # GCM standard

#: Argon2id cost. ~64 MiB / 3 passes keeps unlock well under a second on a
#: laptop while making large-scale offline guessing expensive.
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_ITERATIONS = 3
ARGON2_LANES = 4

#: scrypt fallback, chosen for comparable cost (N must be a power of two).
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1


class VaultError(SSHDeckError):
    """Raised when a vault cannot be read, written or unlocked."""


class WrongPassphrase(VaultError):
    """The passphrase did not decrypt the vault.

    Deliberately indistinguishable from tampering: a failed AEAD tag cannot
    tell the two apart, and pretending otherwise would leak information.
    """


def crypto_available() -> bool:
    """True when this build can create encrypted vaults."""
    return _CRYPTO


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:
        raise VaultError(f"The vault file is damaged ({exc}).")


def _default_kdf() -> Dict[str, Any]:
    if _ARGON2:
        return {"name": "argon2id", "memory_kib": ARGON2_MEMORY_KIB,
                "iterations": ARGON2_ITERATIONS, "lanes": ARGON2_LANES}
    return {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}


def _derive(passphrase: str, salt: bytes, kdf: Dict[str, Any]) -> bytes:
    """Derive the AES key using the parameters recorded in the envelope."""
    if not isinstance(passphrase, str) or passphrase == "":
        raise VaultError("Enter a passphrase.")
    pw = passphrase.encode("utf-8")
    name = str(kdf.get("name", "")).lower()
    if name == "argon2id":
        if not _ARGON2:
            raise VaultError(
                "This vault needs Argon2id, which this build of cryptography "
                "does not provide. Upgrade the 'cryptography' package.")
        return Argon2id(
            salt=salt, length=KEY_BYTES,
            iterations=int(kdf.get("iterations", ARGON2_ITERATIONS)),
            lanes=int(kdf.get("lanes", ARGON2_LANES)),
            memory_cost=int(kdf.get("memory_kib", ARGON2_MEMORY_KIB)),
        ).derive(pw)
    if name == "scrypt":
        if Scrypt is None:
            raise VaultError("This build cannot derive scrypt keys.")
        return Scrypt(salt=salt, length=KEY_BYTES,
                      n=int(kdf.get("n", SCRYPT_N)),
                      r=int(kdf.get("r", SCRYPT_R)),
                      p=int(kdf.get("p", SCRYPT_P))).derive(pw)
    raise VaultError(f"Unsupported key-derivation method {name!r}.")


def _header(salt: bytes, nonce: bytes, kdf: Dict[str, Any],
            hint: str = "") -> Dict[str, Any]:
    # The hint is deliberately outside the ciphertext: it has to be readable
    # by someone who cannot open the vault, which is the entire point of a
    # hint. It is authenticated as associated data, so it can be read but not
    # altered without breaking the tag.
    header = {"magic": MAGIC, "version": VERSION, "kdf": kdf,
              "salt": _b64e(salt), "nonce": _b64e(nonce),
              "cipher": "AES-256-GCM"}
    if hint:
        header["hint"] = hint
    return header


def _aad(header: Dict[str, Any]) -> bytes:
    """Header bytes bound into the AEAD tag, so it cannot be edited."""
    return json.dumps({k: v for k, v in header.items() if k != "payload"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(data: Any, passphrase: str, hint: str = "") -> Dict[str, Any]:
    """Encrypt *data* into an envelope, optionally carrying a plaintext *hint*.

    A hint is a reminder, not a secret: it is stored unencrypted so it can be
    shown on the unlock screen. Anyone with the file can read it, which is why
    the UI warns against making it a paraphrase of the passphrase.
    """
    if not _CRYPTO:
        raise VaultError(
            "Encryption needs the 'cryptography' package, which is missing.")
    kdf = _default_kdf()
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = _header(salt, nonce, kdf, hint)
    key = _derive(passphrase, salt, kdf)
    try:
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        ct = AESGCM(key).encrypt(nonce, plaintext, _aad(header))
    finally:
        del key
    out = dict(header)
    out["payload"] = _b64e(ct)
    return out


def open_envelope(envelope: Dict[str, Any], passphrase: str) -> Any:
    """Decrypt an envelope produced by :func:`seal`."""
    if not _CRYPTO:
        raise VaultError(
            "Decryption needs the 'cryptography' package, which is missing.")
    if not isinstance(envelope, dict) or envelope.get("magic") != MAGIC:
        raise VaultError("That file is not an SSHDeck vault.")
    version = envelope.get("version")
    if version != VERSION:
        raise VaultError(
            f"This vault was written by a newer SSHDeck (format {version}).")
    kdf = envelope.get("kdf")
    if not isinstance(kdf, dict):
        raise VaultError("The vault file is damaged (no key settings).")
    salt = _b64d(str(envelope.get("salt", "")))
    nonce = _b64d(str(envelope.get("nonce", "")))
    ct = _b64d(str(envelope.get("payload", "")))
    header = _header(salt, nonce, kdf, str(envelope.get("hint", "")))
    key = _derive(passphrase, salt, kdf)
    try:
        raw = AESGCM(key).decrypt(nonce, ct, _aad(header))
    except Exception:
        # A bad passphrase and a tampered file fail identically here.
        raise WrongPassphrase("That passphrase did not unlock the vault.")
    finally:
        del key
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise VaultError(f"The vault decrypted but its contents are damaged ({exc}).")


def is_vault_file(path: str) -> bool:
    """True when *path* holds an encrypted vault (cheap header peek)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            head = json.load(fh)
    except Exception:
        return False
    return isinstance(head, dict) and head.get("magic") == MAGIC


def read_hint(path: str) -> str:
    """The password hint stored with the vault, or "" if there is none.

    Readable without the passphrase by design -- the unlock screen shows it to
    someone who cannot get in.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            envelope = json.load(fh)
    except Exception:
        return ""
    if not isinstance(envelope, dict) or envelope.get("magic") != MAGIC:
        return ""
    hint = envelope.get("hint", "")
    return hint if isinstance(hint, str) else ""


def write_vault(path: str, data: Any, passphrase: str, hint: str = "") -> None:
    """Seal *data* to *path*, atomically and readable only by this user."""
    envelope = seal(data, passphrase, hint)
    tmp = path + ".tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Create with 0600 from the outset -- never widen-then-narrow, which
        # would leave a readable window for anything watching the directory.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise VaultError(f"Could not write {path}: {exc}.")


def read_vault(path: str, passphrase: str) -> Any:
    """Read and decrypt the vault at *path*."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            envelope = json.load(fh)
    except FileNotFoundError:
        raise VaultError(f"No vault at {path}.")
    except OSError as exc:
        raise VaultError(f"Could not read {path}: {exc}.")
    except ValueError as exc:
        raise VaultError(f"The vault file is damaged ({exc}).")
    return open_envelope(envelope, passphrase)


def change_passphrase(path: str, old: str, new: str, hint: str = None) -> None:
    """Re-seal the vault under *new*, verifying *old* first.

    Re-derives a fresh salt and nonce, so the new file shares no key material
    with the old one.
    """
    data = read_vault(path, old)
    if not new:
        raise VaultError("Enter the new passphrase.")
    # Keep the existing hint unless a new one is given -- changing the
    # passphrase does not silently strip the reminder.
    keep = read_hint(path) if hint is None else hint
    write_vault(path, data, new, keep)
