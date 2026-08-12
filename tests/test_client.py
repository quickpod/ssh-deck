"""client.connect() error handling against an unreachable port (no server)."""

import socket

import pytest

from sshdeck import client
from sshdeck.sessions import Session
from sshdeck.errors import SSHDeckError


def _closed_port():
    """Bind a socket, read its port, then close it -> a port nothing listens on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connect_unreachable_raises_clean_error():
    port = _closed_port()
    session = Session("dead", "127.0.0.1", port=port, user="nobody", auth="agent")
    with pytest.raises(SSHDeckError):
        client.connect(session, timeout=2)


def test_password_auth_without_password_raises():
    session = Session("pw", "127.0.0.1", port=22, user="me", auth="password")
    with pytest.raises(SSHDeckError):
        client.connect(session)  # no password supplied


def test_run_without_client_raises():
    with pytest.raises(SSHDeckError):
        client.run(None, "uptime")


def test_close_none_is_safe():
    client.close(None)  # must not raise
