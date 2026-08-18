"""sshdeck -- a small, offline SSH/SFTP client library behind the SSHDeck app.

Public surface (all failures raise :class:`SSHDeckError`)::

    from sshdeck import sessions, client, sftp, keys, forward
    s = sessions.Session("web", "example.com", user="deploy")
    conn = client.connect(s, password="...")      # or key/agent auth
    rc, out, err = client.run(conn, "uptime")

Secrets are never written to disk: session profiles store host/user/key-path only,
and passwords/passphrases are supplied at connect time.  Importing this package
pulls in no GUI and touches no network.
"""

from __future__ import annotations

from .errors import SSHDeckError
from . import sessions, keys, forward, client, sftp
from .sessions import Session
from .forward import ForwardSpec, parse_forward

__version__ = "1.3.3"

__all__ = [
    "SSHDeckError",
    "Session",
    "ForwardSpec",
    "parse_forward",
    "sessions",
    "keys",
    "forward",
    "client",
    "sftp",
]
