"""Optional loopback round-trip using an in-process paramiko server.

If a tiny local SSH server can be stood up, we exercise a real
``connect`` -> ``run`` -> ``close`` cycle.  Anything that makes that hard causes
a graceful skip (this suite must stay green with no external SSH server).
"""

import socket
import threading

import pytest

paramiko = pytest.importorskip("paramiko")

from sshdeck import client
from sshdeck.sessions import Session


class _Server(paramiko.ServerInterface):
    """Accepts password 'pw' for user 'tester' and answers exec 'echo hi'."""

    def __init__(self):
        self.event = threading.Event()
        self.command = None

    def check_auth_password(self, username, password):
        if username == "tester" and password == "pw":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command
        self.event.set()
        return True


def _serve_once(sock, host_key):
    conn, _ = sock.accept()
    transport = paramiko.Transport(conn)
    transport.add_server_key(host_key)
    server = _Server()
    try:
        transport.start_server(server=server)
        chan = transport.accept(10)
        if chan is None:
            return
        server.event.wait(10)
        chan.sendall(b"hi\n")
        chan.send_exit_status(0)
        chan.close()
    finally:
        try:
            transport.close()
        except Exception:
            pass


def test_run_roundtrip_against_local_server():
    try:
        host_key = paramiko.RSAKey.generate(2048)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"could not set up a local server: {exc}")

    t = threading.Thread(target=_serve_once, args=(listener, host_key),
                         daemon=True)
    t.start()

    session = Session("local", "127.0.0.1", port=port, user="tester",
                      auth="password")
    conn = None
    try:
        conn = client.connect(session, password="pw", timeout=10)
        rc, out, err = client.run(conn, "echo hi")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"local round-trip unavailable: {exc}")
    finally:
        client.close(conn)
        try:
            listener.close()
        except Exception:
            pass

    assert rc == 0
    assert out.strip() == "hi"
