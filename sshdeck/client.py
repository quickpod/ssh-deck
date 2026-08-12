r"""Connecting to hosts and running commands (paramiko).

:func:`connect` turns a :class:`sshdeck.sessions.Session` into a live paramiko
``SSHClient``, honouring the profile's auth method (key / password / agent) and an
optional jump host (an OpenSSH ``ProxyJump``, implemented with a ``direct-tcpip``
channel through the jump connection).  Failures -- auth rejected, host
unreachable, timeout -- are translated into a clean :class:`SSHDeckError` message
rather than a paramiko/socket traceback.

:func:`run` executes one command and returns ``(returncode, stdout, stderr)``.
"""

from __future__ import annotations

import socket

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError

from .errors import SSHDeckError
from .keys import load_key

DEFAULT_TIMEOUT = 15


def _parse_jump(spec, default_user=None):
    """Parse a jump spec ``[user@]host[:port]`` into ``(user, host, port)``."""
    user, host, port = default_user, spec, 22
    if "@" in host:
        user, host = host.split("@", 1)
    if ":" in host:
        host, port_s = host.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            raise SSHDeckError(f"invalid jump-host port in {spec!r}")
    if not host:
        raise SSHDeckError(f"invalid jump host {spec!r}")
    return (user or default_user), host, port


def _open_jump_channel(session, password, passphrase, timeout):
    """Connect to the jump host and return (jump_client, sock) to the target."""
    juser, jhost, jport = _parse_jump(session.jump, session.user)
    jump = paramiko.SSHClient()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        jump.connect(hostname=jhost, port=jport, username=juser,
                     password=password, passphrase=passphrase,
                     timeout=timeout, allow_agent=True, look_for_keys=True)
    except Exception as exc:
        _raise_connect_error(exc, f"{juser or ''}@{jhost}:{jport}", jump=True)
    try:
        transport = jump.get_transport()
        sock = transport.open_channel(
            "direct-tcpip", (session.host, session.port), ("127.0.0.1", 0))
    except Exception as exc:
        jump.close()
        raise SSHDeckError(
            f"jump host {jhost} could not reach {session.host}:{session.port} "
            f"({exc})")
    return jump, sock


def connect(session, password=None, passphrase=None, timeout=DEFAULT_TIMEOUT):
    """Open and return a connected paramiko ``SSHClient`` for *session*.

    The returned client carries a ``_sshdeck_jump`` attribute (the jump client,
    or ``None``) so :func:`close` can tear the whole chain down.
    """
    if session is None:
        raise SSHDeckError("no session provided")
    if session.auth == "password" and not password:
        raise SSHDeckError(
            f"session {session.name!r} uses password auth -- a password is required")

    pkey = None
    if session.auth == "key" and session.key_path:
        pkey = load_key(session.key_path, passphrase=passphrase)

    jump_client = None
    sock = None
    if session.jump:
        jump_client, sock = _open_jump_channel(session, password, passphrase,
                                               timeout)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=session.host,
        port=session.port,
        username=session.user,
        timeout=timeout,
        sock=sock,
    )
    if session.auth == "key":
        connect_kwargs.update(pkey=pkey, password=None, passphrase=passphrase,
                              allow_agent=False, look_for_keys=(pkey is None))
    elif session.auth == "password":
        connect_kwargs.update(password=password, allow_agent=False,
                              look_for_keys=False)
    else:  # agent
        connect_kwargs.update(allow_agent=True, look_for_keys=True)

    try:
        client.connect(**connect_kwargs)
    except Exception as exc:
        if jump_client is not None:
            jump_client.close()
        _raise_connect_error(exc, session.target())

    client._sshdeck_jump = jump_client  # type: ignore[attr-defined]
    return client


def _raise_connect_error(exc, target, jump=False):
    """Translate a paramiko/socket exception into a clean SSHDeckError."""
    where = f"jump host {target}" if jump else target
    if isinstance(exc, paramiko.AuthenticationException):
        raise SSHDeckError(f"authentication failed for {where}")
    if isinstance(exc, paramiko.BadHostKeyException):
        raise SSHDeckError(f"host key verification failed for {where}")
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        raise SSHDeckError(f"timed out connecting to {where}")
    if isinstance(exc, NoValidConnectionsError):
        raise SSHDeckError(f"could not connect to {where} (connection refused)")
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)):
        raise SSHDeckError(f"could not connect to {where} ({exc})")
    if isinstance(exc, paramiko.SSHException):
        raise SSHDeckError(f"SSH error connecting to {where}: {exc}")
    raise SSHDeckError(f"could not connect to {where}: {exc}")


def run(client, cmd, timeout=None):
    """Run *cmd* on *client*; return ``(returncode, stdout, stderr)`` as text."""
    if client is None:
        raise SSHDeckError("not connected")
    if not cmd or not str(cmd).strip():
        raise SSHDeckError("no command given")
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdin.close()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    except SSHDeckError:
        raise
    except Exception as exc:
        raise SSHDeckError(f"command failed: {exc}")


def open_shell(client, term="xterm", width=80, height=24):
    """Open an interactive shell channel (used by the GUI terminal)."""
    if client is None:
        raise SSHDeckError("not connected")
    try:
        chan = client.invoke_shell(term=term, width=width, height=height)
        chan.settimeout(0.0)
        return chan
    except Exception as exc:
        raise SSHDeckError(f"could not open a shell: {exc}")


def close(client):
    """Close *client* and any jump connection behind it (never raises)."""
    if client is None:
        return
    jump = getattr(client, "_sshdeck_jump", None)
    try:
        client.close()
    except Exception:
        pass
    if jump is not None:
        try:
            jump.close()
        except Exception:
            pass
