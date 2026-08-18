r"""Tiny JSON-backed config for SSHDeck (theme + recent sessions).

Stores just two things and never raises: the chosen theme ("light"/"dark") and a
short list of recently-connected session names.  On Windows the file lives at
``%LOCALAPPDATA%\SSHDeck\config.json``; elsewhere it falls back to
``~/.sshdeck/config.json``.  The same directory is used by :mod:`sshdeck.sessions`
for the session store, so :func:`config_dir` is the one shared location.

Every function is defensive -- a corrupt or unreadable config must never stop the
app from starting.
"""

from __future__ import annotations

import json
import os

APP_DIRNAME = "SSHDeck"
CONFIG_NAME = "config.json"
MAX_RECENT = 10
VALID_THEMES = ("light", "dark")


def config_dir():
    r"""Directory that holds config + session data (created on demand).

    ``%LOCALAPPDATA%\SSHDeck`` on Windows, ``~/.sshdeck`` otherwise.  Honours an
    ``SSHDECK_CONFIG_DIR`` override (handy for tests and portable installs).
    """
    override = os.environ.get("SSHDECK_CONFIG_DIR")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local and os.name == "nt":
        return os.path.join(local, APP_DIRNAME)
    return os.path.join(os.path.expanduser("~"), "." + APP_DIRNAME.lower())


def config_path():
    return os.path.join(config_dir(), CONFIG_NAME)


def _defaults():
    return {"theme": "light", "recent": [], "global_key": "",
            "master_prompt_seen": False}


def load():
    """Return the config dict, always with ``theme`` and ``recent`` keys."""
    cfg = _defaults()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in VALID_THEMES:
                cfg["theme"] = theme
            recent = data.get("recent")
            if isinstance(recent, list):
                cfg["recent"] = [p for p in recent if isinstance(p, str)][:MAX_RECENT]
            gk = data.get("global_key")
            if isinstance(gk, str):
                cfg["global_key"] = gk
            cfg["master_prompt_seen"] = bool(data.get("master_prompt_seen", False))
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    """Persist *cfg* (best-effort; failures are swallowed)."""
    try:
        os.makedirs(config_dir(), exist_ok=True)
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "light",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
            "global_key": cfg.get("global_key", "") if isinstance(cfg.get("global_key", ""), str) else "",
            "master_prompt_seen": bool(cfg.get("master_prompt_seen", False)),
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    return load().get("theme", "light")


def set_theme(theme):
    if theme not in VALID_THEMES:
        return
    cfg = load()
    cfg["theme"] = theme
    save(cfg)


def get_recent():
    return load().get("recent", [])


def add_recent(name):
    """Push a session *name* to the front of the recent list (dedup, capped)."""
    if not name:
        return
    cfg = load()
    recent = [p for p in cfg.get("recent", []) if p != name]
    recent.insert(0, name)
    cfg["recent"] = recent[:MAX_RECENT]
    save(cfg)


def clear_recent():
    cfg = load()
    cfg["recent"] = []
    save(cfg)


def get_global_key():
    """Path of the identity key new sessions inherit ("" when unset).

    A session's own ``key_path`` always takes precedence; this is the
    fall-back, equivalent to SecureCRT's global public key.
    """
    return load().get("global_key", "")


def set_global_key(path):
    cfg = load()
    cfg["global_key"] = str(path or "")
    save(cfg)


def get_master_prompt_seen():
    """True once the first-run master-password offer has been shown.

    Asking every launch would be nagging; asking once is an offer.
    """
    return bool(load().get("master_prompt_seen", False))


def set_master_prompt_seen(flag):
    cfg = load()
    cfg["master_prompt_seen"] = bool(flag)
    save(cfg)
