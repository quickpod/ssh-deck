r"""SSH key-pair generation and inspection (paramiko + cryptography).

Everything here is fully testable without a server:

* :func:`generate_keypair` writes an OpenSSH private key and a ``.pub`` sibling
  for ``ed25519`` (via :mod:`cryptography`, since paramiko cannot yet *generate*
  Ed25519) or ``rsa`` (via paramiko).  An optional passphrase encrypts the
  private key at rest.
* :func:`load_key` loads a private key back (trying each supported type).
* :func:`public_key_string` returns the one-line ``ssh-ed25519 AAAA...`` /
  ``ssh-rsa AAAA...`` authorized_keys form, optionally with a comment.
"""

from __future__ import annotations

import io
import os

import paramiko

from .errors import SSHDeckError

KEY_TYPES = ("ed25519", "rsa", "ecdsa")
DEFAULT_RSA_BITS = 3072

# paramiko private-key classes we know how to load.
_LOADERS = (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey)


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:
            raise SSHDeckError(f"could not create directory {parent}: {exc}")


def generate_keypair(type="ed25519", path=None, passphrase=None, bits=None,
                     comment=None):
    """Generate a key pair and write ``path`` (private) + ``path.pub`` (public).

    ``type`` is ``ed25519`` (default), ``rsa`` or ``ecdsa``.  ``passphrase`` (if
    given) encrypts the private key on disk.  Returns the private-key path.
    """
    if not path:
        raise SSHDeckError("an output path is required")
    ktype = (type or "ed25519").strip().lower()
    if ktype not in KEY_TYPES:
        raise SSHDeckError(
            f"key type must be one of {', '.join(KEY_TYPES)}, got {type!r}")
    _ensure_parent(path)
    passphrase = passphrase or None

    try:
        if ktype == "ed25519":
            key = _generate_ed25519(path, passphrase)
        elif ktype == "rsa":
            key = paramiko.RSAKey.generate(int(bits or DEFAULT_RSA_BITS))
            key.write_private_key_file(path, password=passphrase)
        else:  # ecdsa
            key = paramiko.ECDSAKey.generate()
            key.write_private_key_file(path, password=passphrase)
    except SSHDeckError:
        raise
    except Exception as exc:
        raise SSHDeckError(f"could not generate {ktype} key: {exc}")

    try:
        os.chmod(path, 0o600)
    except Exception:
        pass  # best-effort on platforms without POSIX perms

    pub = _public_line(key, comment)
    pub_path = path + ".pub"
    try:
        with open(pub_path, "w", encoding="utf-8") as fh:
            fh.write(pub + "\n")
    except Exception as exc:
        raise SSHDeckError(f"could not write public key {pub_path}: {exc}")
    return path


def _generate_ed25519(path, passphrase):
    """Generate an Ed25519 key with cryptography, load it back via paramiko."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except Exception as exc:  # pragma: no cover - cryptography ships with paramiko
        raise SSHDeckError(f"cryptography is required for ed25519 keys: {exc}")

    priv = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
           if passphrase else serialization.NoEncryption())
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=enc,
    )
    with open(path, "wb") as fh:
        fh.write(pem)
    # Load back through paramiko so the returned object matches the other paths.
    return paramiko.Ed25519Key.from_private_key(
        io.StringIO(pem.decode("utf-8")), password=passphrase)


def load_key(path, passphrase=None):
    """Load a private key from *path*, trying each supported key type.

    Raises :class:`SSHDeckError` with a clean message on a missing file, a wrong
    passphrase, or an unrecognised format.
    """
    if not path or not os.path.exists(path):
        raise SSHDeckError(f"key file not found: {path}")
    passphrase = passphrase or None
    saw_password_error = False
    for loader in _LOADERS:
        try:
            return loader.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            saw_password_error = True
        except paramiko.SSHException:
            continue
        except Exception:
            continue
    if saw_password_error:
        raise SSHDeckError(
            f"{path} is encrypted -- a passphrase is required (or it is wrong)")
    raise SSHDeckError(f"could not load a private key from {path} "
                       "(unsupported format or wrong passphrase)")


def public_key_string(path, passphrase=None, comment=None):
    """Return the authorized_keys one-liner for the key at *path*.

    *path* may point at either the private key or an existing ``.pub`` file.
    """
    if path and path.endswith(".pub") and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                line = fh.read().strip()
            if line:
                return line
        except Exception as exc:
            raise SSHDeckError(f"could not read {path}: {exc}")
    key = load_key(path, passphrase=passphrase)
    return _public_line(key, comment)


def _public_line(key, comment=None):
    """Format a loaded paramiko key as ``<type> <base64> [comment]``."""
    line = f"{key.get_name()} {key.get_base64()}"
    if comment:
        line += f" {comment}"
    return line
