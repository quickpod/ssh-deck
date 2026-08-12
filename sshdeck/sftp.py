r"""SFTP operations over a connected paramiko client.

:func:`open_sftp` returns a small :class:`SFTP` wrapper that turns paramiko's
``SFTPClient`` calls into clean :class:`SSHDeckError` failures and adds a couple
of conveniences the GUI needs (an ``Entry`` list for a directory, size-aware
transfers with a progress callback).  Nothing here touches the network at import
time, so the module imports fine on a headless box.
"""

from __future__ import annotations

import os
import stat as stat_mod

from .errors import SSHDeckError


class Entry:
    """One directory entry: name, whether it is a directory, size, mode."""

    __slots__ = ("name", "is_dir", "size", "mode", "mtime")

    def __init__(self, name, is_dir, size, mode, mtime=0):
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.mode = mode
        self.mtime = mtime

    def __repr__(self):
        kind = "dir" if self.is_dir else "file"
        return f"Entry({self.name!r}, {kind}, {self.size})"


def open_sftp(client):
    """Open an SFTP channel on a connected client and return an :class:`SFTP`."""
    if client is None:
        raise SSHDeckError("not connected")
    try:
        return SFTP(client.open_sftp())
    except Exception as exc:
        raise SSHDeckError(f"could not open SFTP: {exc}")


class SFTP:
    """Thin wrapper around ``paramiko.SFTPClient`` with clean error handling."""

    def __init__(self, sftp):
        self._sftp = sftp

    # -- listing / stat --------------------------------------------------
    def listdir(self, path="."):
        """Return a list of :class:`Entry`, directories first then by name."""
        try:
            attrs = self._sftp.listdir_attr(path)
        except IOError as exc:
            raise SSHDeckError(f"could not list {path}: {exc}")
        except Exception as exc:
            raise SSHDeckError(f"could not list {path}: {exc}")
        entries = []
        for a in attrs:
            is_dir = stat_mod.S_ISDIR(a.st_mode or 0)
            entries.append(Entry(a.filename, is_dir, a.st_size or 0,
                                 a.st_mode or 0, a.st_mtime or 0))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, path):
        try:
            a = self._sftp.stat(path)
        except IOError as exc:
            raise SSHDeckError(f"could not stat {path}: {exc}")
        name = os.path.basename(path.rstrip("/")) or path
        return Entry(name, stat_mod.S_ISDIR(a.st_mode or 0), a.st_size or 0,
                     a.st_mode or 0, a.st_mtime or 0)

    def exists(self, path):
        try:
            self._sftp.stat(path)
            return True
        except IOError:
            return False
        except Exception:
            return False

    def getcwd(self):
        try:
            return self._sftp.normalize(".")
        except Exception:
            return "."

    # -- transfers -------------------------------------------------------
    def get(self, remote, local, callback=None):
        """Download *remote* to *local*.  ``callback(done, total)`` is optional."""
        try:
            self._sftp.get(remote, local, callback=callback)
        except IOError as exc:
            raise SSHDeckError(f"could not download {remote}: {exc}")
        except Exception as exc:
            raise SSHDeckError(f"could not download {remote}: {exc}")
        return local

    def put(self, local, remote, callback=None):
        """Upload *local* to *remote*.  ``callback(done, total)`` is optional."""
        if not os.path.exists(local):
            raise SSHDeckError(f"local file not found: {local}")
        try:
            self._sftp.put(local, remote, callback=callback)
        except IOError as exc:
            raise SSHDeckError(f"could not upload {local}: {exc}")
        except Exception as exc:
            raise SSHDeckError(f"could not upload {local}: {exc}")
        return remote

    # -- mutations -------------------------------------------------------
    def mkdir(self, path, mode=0o755):
        try:
            self._sftp.mkdir(path, mode)
        except IOError as exc:
            raise SSHDeckError(f"could not create directory {path}: {exc}")
        return path

    def remove(self, path):
        """Remove a file, or an (empty) directory if *path* is one."""
        try:
            try:
                a = self._sftp.stat(path)
            except IOError as exc:
                raise SSHDeckError(f"no such path: {path}")
            if stat_mod.S_ISDIR(a.st_mode or 0):
                self._sftp.rmdir(path)
            else:
                self._sftp.remove(path)
        except SSHDeckError:
            raise
        except IOError as exc:
            raise SSHDeckError(f"could not remove {path}: {exc}")
        return path

    def rename(self, old, new):
        try:
            self._sftp.rename(old, new)
        except IOError as exc:
            raise SSHDeckError(f"could not rename {old} -> {new}: {exc}")
        return new

    def close(self):
        try:
            self._sftp.close()
        except Exception:
            pass
