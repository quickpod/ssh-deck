r"""Import saved sessions from SecureCRT.

SecureCRT keeps one ``.ini`` per session under ``Config/Sessions``, with typed
keys -- ``S:`` string, ``D:`` dword (8 hex digits), ``B:`` binary blob spread
over continuation lines.  Sub-folders on disk are the folder tree the user
sees, so the relative path becomes the imported profile's folder.

**This module only ever reads.**  SecureCRT's own files are never written,
moved or locked, so an import cannot damage the user's existing setup and can
be repeated safely.

Saved passwords are deliberately *not* imported.  They are stored as
``03:``-prefixed blobs encrypted under SecureCRT's configuration passphrase;
reading them would mean reimplementing that scheme and demanding the master
passphrase.  Instead each result records whether a password *was* saved
(:attr:`Imported.had_password`) so the app can tell the user exactly which
profiles still need one typed in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import SSHDeckError
from .sessions import Session

#: ``S:"Key"=value`` / ``D:"Key"=0000001a`` / ``B:"Key"=0000000a``
_ENTRY = re.compile(r'^([SDB]):"([^"]*)"=(.*)$')

FOLDER_DATA = "__FolderData__.ini"
#: SecureCRT ships these; importing them adds noise, not sessions.
SKIP_STEMS = {"Default", "Default_LocalShell"}

DEFAULT_SSH_PORT = 22


class ImportError_(SSHDeckError):
    """Raised when a SecureCRT configuration cannot be read."""


@dataclass
class Imported:
    """One SecureCRT session translated into an SSHDeck profile."""

    session: Session
    folder: str = ""                 # "" = top level
    had_password: bool = False       # a password was saved, but not importable
    protocol: str = ""               # SSH2, Local Shell, telnet, ...
    source: str = ""                 # the .ini it came from
    notes: List[str] = field(default_factory=list)

    @property
    def needs_password(self) -> bool:
        return self.had_password or self.session.auth == "password"


def default_config_dir() -> Optional[str]:
    r"""SecureCRT's ``Config`` directory for the current user, or None.

    Checks the documented per-platform locations; VanDyke uses ``.vandyke`` on
    Linux/macOS and ``AppData\Roaming`` on Windows.
    """
    candidates = []
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "VanDyke", "Config"))
    candidates += [
        os.path.join(home, ".vandyke", "SecureCRT", "Config"),
        os.path.join(home, "Library", "Application Support", "VanDyke",
                     "SecureCRT", "Config"),
    ]
    for path in candidates:
        if os.path.isdir(os.path.join(path, "Sessions")):
            return path
    return None


def parse_ini(text: str) -> Dict[str, Tuple[str, str]]:
    """Parse SecureCRT's typed ini into ``{key: (type, raw_value)}``.

    Binary blobs continue onto indented lines; those are skipped rather than
    reassembled, since nothing we import is binary.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for line in text.splitlines():
        line = line.lstrip("﻿")
        if not line or line.startswith(" "):     # blob continuation
            continue
        m = _ENTRY.match(line)
        if m:
            kind, key, value = m.group(1), m.group(2), m.group(3)
            out[key] = (kind, value)
    return out


def _s(entries, key, default="") -> str:
    kind_value = entries.get(key)
    return kind_value[1].strip() if kind_value else default


def _d(entries, key, default=0) -> int:
    """A ``D:`` dword, stored as 8 hex digits."""
    kind_value = entries.get(key)
    if not kind_value:
        return default
    try:
        return int(kind_value[1].strip(), 16)
    except ValueError:
        return default


def _auth_for(entries) -> str:
    """Map SecureCRT's auth settings onto SSHDeck's key/password/agent.

    SecureCRT lists several methods in preference order and tries them in
    turn.  SSHDeck records one, so pick the one the user most likely relies
    on: a saved password means password auth; otherwise publickey if offered.
    """
    if _d(entries, "Session Password Saved") == 1:
        return "password"
    methods = [m.strip().lower()
               for m in _s(entries, "SSH2 Authentications V2").split(",")
               if m.strip()]
    if "publickey" in methods:
        return "key"
    if "password" in methods or "keyboard-interactive" in methods:
        return "password"
    return "key"


def _jump_for(entries) -> Optional[str]:
    """SecureCRT's per-session firewall, when it names a real one."""
    name = _s(entries, "Firewall Name")
    if not name or name.lower() in ("none", "default"):
        return None
    return name


def read_session_file(path: str, folder: str = "") -> Optional[Imported]:
    """Translate one ``.ini`` into an :class:`Imported`, or None to skip it."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            entries = parse_ini(fh.read())
    except OSError as exc:
        raise ImportError_(f"Could not read {path}: {exc}.")

    name = os.path.splitext(os.path.basename(path))[0]
    protocol = _s(entries, "Protocol Name")
    host = _s(entries, "Hostname")
    notes: List[str] = []

    # Local Shell sessions have no host and nothing to connect to remotely.
    if not host:
        return None

    port = _d(entries, "Port", DEFAULT_SSH_PORT) or DEFAULT_SSH_PORT
    user = _s(entries, "Username") or None
    had_password = _d(entries, "Session Password Saved") == 1
    if had_password:
        notes.append("SecureCRT had a saved password; it could not be "
                     "imported and must be re-entered.")
    if protocol and protocol.upper() not in ("SSH2", "SSH1"):
        notes.append(f"SecureCRT protocol was {protocol!r}; imported as SSH.")

    # SecureCRT's "Use Global Public Key" means "inherit"; SSHDeck expresses
    # that as an empty key_path, so only a genuine per-session override is
    # recorded here.
    key_path = None
    if _d(entries, "Use Global Public Key", 1) != 1:
        override = _s(entries, "Identity Filename V2") or _s(entries, "Identity Filename")
        if override:
            key_path = expand_vds(override, os.path.dirname(os.path.dirname(path)))
            notes.append(f"Uses its own key: {key_path}")

    try:
        session = Session(name=name, host=host, port=port, user=user,
                          auth=_auth_for(entries), key_path=key_path,
                          jump=_jump_for(entries))
    except SSHDeckError as exc:
        raise ImportError_(f"{name}: {exc}")

    return Imported(session=session, folder=folder, had_password=had_password,
                    protocol=protocol, source=path, notes=notes)


def expand_vds(path: str, config_dir: str) -> str:
    r"""Resolve SecureCRT's ``${VDS_*}`` variables in a configured path.

    SecureCRT resolves ``VDS_USER_DATA_PATH`` to the user's home directory
    (``${VDS_USER_DATA_PATH}/Documents/x.pk`` is ``~/Documents/x.pk``), and
    ``VDS_SSH_DATA_PATH`` to the OpenSSH key folder.  Unknown variables are
    left alone rather than mangled, so a path we do not understand stays
    recognisable to the user instead of silently pointing somewhere wrong.
    """
    text = (path or "").strip()
    if not text:
        return ""
    home = os.path.expanduser("~")
    mapping = {
        "VDS_USER_DATA_PATH": home,
        "VDS_SSH_DATA_PATH": os.path.join(home, ".ssh"),
        "VDS_CONFIG_PATH": config_dir,
        "VDS_APP_DATA_PATH": os.path.dirname(os.path.abspath(config_dir)),
    }
    for var, value in mapping.items():
        text = text.replace("${%s}" % var, value).replace("$%s" % var, value)
    text = os.path.expanduser(os.path.expandvars(text))
    return os.path.normpath(text)


def global_identity_key(config_dir: str) -> Optional[str]:
    r"""The key every session inherits, from ``SSH2.ini``'s Identity Filename.

    Returns the resolved path when it exists on disk.  A configured-but-absent
    key still comes back, so the caller can tell the user their global key is
    missing rather than silently importing nothing.
    """
    path = os.path.join(config_dir, "SSH2.ini")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            entries = parse_ini(fh.read())
    except OSError:
        return None
    for key in ("Identity Filename V2", "Identity Filename"):
        raw = _s(entries, key)
        if raw:
            resolved = expand_vds(raw, config_dir)
            if resolved:
                return resolved
    return None


def read_folder_order(config_dir: str) -> List[str]:
    """The session order SecureCRT shows, from ``__FolderData__.ini``.

    Purely cosmetic; a missing or unreadable file just means "no preference".
    """
    path = os.path.join(config_dir, "Sessions", FOLDER_DATA)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            entries = parse_ini(fh.read())
    except OSError:
        return []
    return [s for s in _s(entries, "Session List").split(":") if s]


def discover(config_dir: Optional[str] = None) -> List[Imported]:
    """Read every session under *config_dir*, in SecureCRT's own order.

    Sub-directories become folders.  Unreadable or unusable individual
    sessions are skipped rather than failing the whole import -- one bad file
    should not cost the user the other twenty-six.
    """
    config_dir = config_dir or default_config_dir()
    if not config_dir:
        raise ImportError_(
            "No SecureCRT configuration found. Point the import at the folder "
            "containing 'Sessions' (SecureCRT shows it under "
            "Options > Global Options > Configuration Paths).")
    root = os.path.join(config_dir, "Sessions")
    if not os.path.isdir(root):
        raise ImportError_(f"{config_dir} has no 'Sessions' folder.")

    found: List[Imported] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        folder = os.path.relpath(dirpath, root)
        folder = "" if folder == "." else folder.replace(os.sep, "/")
        for filename in sorted(filenames):
            if not filename.lower().endswith(".ini"):
                continue
            stem = os.path.splitext(filename)[0]
            if stem == os.path.splitext(FOLDER_DATA)[0] or stem in SKIP_STEMS:
                continue
            try:
                item = read_session_file(os.path.join(dirpath, filename), folder)
            except ImportError_:
                continue
            if item is not None:
                found.append(item)

    order = {name: i for i, name in enumerate(read_folder_order(config_dir))}
    found.sort(key=lambda i: (order.get(i.session.name, len(order)),
                              i.session.name.lower()))
    return found
