r"""Port-forward specifications -- parsing/description (testable) + setup (guarded).

An SSH port forward is described by a compact spec, exactly like OpenSSH's ``-L``
and ``-R``:

* local  (``-L``):  ``[bind_host:]bind_port:dest_host:dest_port``
* remote (``-R``):  ``[bind_host:]bind_port:dest_host:dest_port``

The parsing and description of these specs is pure and unit-tested.  The actual
setup on a live connection lives in :func:`start_forward` and is guarded so this
module imports (and its spec logic runs) without any network.
"""

from __future__ import annotations

from .errors import SSHDeckError

KINDS = ("local", "remote")
DEFAULT_BIND = "127.0.0.1"


class ForwardSpec:
    """A parsed port-forward: kind + bind host/port + destination host/port."""

    __slots__ = ("kind", "bind_host", "bind_port", "dest_host", "dest_port")

    def __init__(self, kind, bind_host, bind_port, dest_host, dest_port):
        self.kind = kind
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.dest_host = dest_host
        self.dest_port = dest_port

    def to_dict(self):
        return {
            "kind": self.kind,
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "dest_host": self.dest_host,
            "dest_port": self.dest_port,
        }

    def describe(self):
        """A human-readable one-line description of the tunnel direction."""
        left = f"{self.bind_host}:{self.bind_port}"
        right = f"{self.dest_host}:{self.dest_port}"
        if self.kind == "local":
            return (f"local {left}  ->  {right} (via server): "
                    f"connect locally to {left} to reach {right}")
        return (f"remote {left}  ->  {right} (via client): "
                f"the server's {left} reaches {right} on your side")

    def __repr__(self):
        return (f"ForwardSpec({self.kind} {self.bind_host}:{self.bind_port}"
                f"->{self.dest_host}:{self.dest_port})")

    def __eq__(self, other):
        return isinstance(other, ForwardSpec) and self.to_dict() == other.to_dict()


def _port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise SSHDeckError(f"port must be a whole number, got {value!r}")
    if not (1 <= port <= 65535):
        raise SSHDeckError(f"port out of range (1-65535): {port}")
    return port


def parse_forward(spec, kind="local"):
    """Parse ``[bind_host:]bind_port:dest_host:dest_port`` into a :class:`ForwardSpec`.

    Accepts 3 fields (bind host defaults to ``127.0.0.1``) or 4 fields.  Examples::

        parse_forward("8080:localhost:80")
        parse_forward("127.0.0.1:5432:db.internal:5432", kind="local")
    """
    kind = (kind or "local").strip().lower()
    if kind not in KINDS:
        raise SSHDeckError(f"kind must be 'local' or 'remote', got {kind!r}")
    if not spec or not str(spec).strip():
        raise SSHDeckError("empty forward spec")
    parts = str(spec).strip().split(":")
    if len(parts) == 3:
        bind_host = DEFAULT_BIND
        bind_port, dest_host, dest_port = parts
    elif len(parts) == 4:
        bind_host, bind_port, dest_host, dest_port = parts
    else:
        raise SSHDeckError(
            f"forward spec must be 'port:host:port' or 'host:port:host:port', "
            f"got {spec!r}")
    bind_host = bind_host.strip() or DEFAULT_BIND
    dest_host = dest_host.strip()
    if not dest_host:
        raise SSHDeckError(f"destination host is missing in {spec!r}")
    return ForwardSpec(kind, bind_host, _port(bind_port), dest_host,
                       _port(dest_port))


def describe(spec, kind="local"):
    """Convenience: parse *spec* (str or :class:`ForwardSpec`) and describe it."""
    fwd = spec if isinstance(spec, ForwardSpec) else parse_forward(spec, kind)
    return fwd.describe()


# ---------------------------------------------------------------------------
# Live setup -- guarded; requires a connected paramiko client/transport.
# ---------------------------------------------------------------------------
def start_forward(client, spec, kind="local"):
    """Set up *spec* on a connected paramiko ``SSHClient``.

    For a **local** forward this spins up a small threaded listener that accepts
    connections on ``bind_host:bind_port`` and channels them through the server to
    the destination.  For a **remote** forward it asks the server to listen and
    routes incoming channels back out locally.  Returns a callable that stops the
    forward.  Any failure is reported as :class:`SSHDeckError`.
    """
    fwd = spec if isinstance(spec, ForwardSpec) else parse_forward(spec, kind)
    transport = getattr(client, "get_transport", lambda: None)()
    if transport is None or not transport.is_active():
        raise SSHDeckError("not connected -- open a session before forwarding")
    try:
        if fwd.kind == "remote":
            return _start_remote(transport, fwd)
        return _start_local(transport, fwd)
    except SSHDeckError:
        raise
    except Exception as exc:
        raise SSHDeckError(f"could not set up {fwd.kind} forward: {exc}")


def _start_local(transport, fwd):  # pragma: no cover - needs a live server
    import select
    import socket
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((fwd.bind_host, fwd.bind_port))
    server.listen(16)
    stop = threading.Event()

    def handle(sock):
        try:
            chan = transport.open_channel(
                "direct-tcpip", (fwd.dest_host, fwd.dest_port),
                sock.getpeername())
        except Exception:
            sock.close()
            return
        while not stop.is_set():
            r, _, _ = select.select([sock, chan], [], [], 1.0)
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                sock.sendall(data)
        chan.close()
        sock.close()

    def accept_loop():
        while not stop.is_set():
            try:
                server.settimeout(1.0)
                sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(sock,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    def stopper():
        stop.set()
        try:
            server.close()
        except Exception:
            pass

    return stopper


def _start_remote(transport, fwd):  # pragma: no cover - needs a live server
    import socket
    import threading

    stop = threading.Event()

    def handler(chan, origin, server_addr):
        try:
            sock = socket.create_connection((fwd.dest_host, fwd.dest_port))
        except Exception:
            chan.close()
            return

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    src.close()
                except Exception:
                    pass

        threading.Thread(target=pump, args=(chan, sock), daemon=True).start()
        threading.Thread(target=pump, args=(sock, chan), daemon=True).start()

    transport.request_port_forward(fwd.bind_host, fwd.bind_port, handler)

    def stopper():
        stop.set()
        try:
            transport.cancel_port_forward(fwd.bind_host, fwd.bind_port)
        except Exception:
            pass

    return stopper
