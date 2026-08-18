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

    def put_tree(self, local_dir, remote_dir, callback=None, on_file=None):
        """Upload a directory recursively, creating remote folders as needed.

        ``callback(done, total)`` reports bytes across the whole tree, not per
        file, so a progress bar advances smoothly through a folder of many
        small files instead of snapping back to zero on each one.
        ``on_file(rel_path)`` is called as each file starts.
        """
        local_dir = os.path.abspath(local_dir)
        if not os.path.isdir(local_dir):
            raise SSHDeckError(f"not a directory: {local_dir}")

        files, total = [], 0
        for root, _dirs, names in os.walk(local_dir):
            for name in names:
                full = os.path.join(root, name)
                try:
                    total += os.path.getsize(full)
                except OSError:
                    continue
                files.append((full, os.path.relpath(full, local_dir)))

        base = remote_dir.rstrip("/") + "/" + os.path.basename(local_dir)
        self.makedirs(base)
        sent = 0
        for full, rel in files:
            target = base + "/" + rel.replace(os.sep, "/")
            parent = target.rsplit("/", 1)[0]
            self.makedirs(parent)
            if on_file:
                on_file(rel)
            start = sent

            def per_file(done, _size, _start=start):
                if callback and total:
                    callback(min(_start + done, total), total)

            self.put(full, target, callback=per_file if callback else None)
            try:
                sent += os.path.getsize(full)
            except OSError:
                pass
        if callback and total:
            callback(total, total)
        return len(files)

    def get_tree(self, remote_dir, local_dir, callback=None, on_file=None):
        """Download a directory recursively, mirroring it under *local_dir*."""
        base = os.path.join(local_dir, remote_dir.rstrip("/").rsplit("/", 1)[-1])

        # Walk the remote side first so the total is known before transferring;
        # without it there is no denominator for progress.
        files, total = [], 0
        stack = [(remote_dir.rstrip("/"), "")]
        while stack:
            rpath, rel = stack.pop()
            for entry in self.listdir(rpath):
                child = rpath + "/" + entry.name
                crel = (rel + "/" + entry.name) if rel else entry.name
                if entry.is_dir:
                    stack.append((child, crel))
                else:
                    files.append((child, crel, entry.size))
                    total += entry.size or 0

        os.makedirs(base, exist_ok=True)
        got = 0
        for rpath, rel, _size in files:
            target = os.path.join(base, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if on_file:
                on_file(rel)
            start = got

            def per_file(done, _size2, _start=start):
                if callback and total:
                    callback(min(_start + done, total), total)

            self.get(rpath, target, callback=per_file if callback else None)
            try:
                got += os.path.getsize(target)
            except OSError:
                pass
        if callback and total:
            callback(total, total)
        return len(files)

    def makedirs(self, path):
        """Create *path* and any missing parents (remote ``mkdir -p``)."""
        parts = [p for p in path.strip("/").split("/") if p]
        cur = "/" if path.startswith("/") else ""
        for part in parts:
            cur = (cur.rstrip("/") + "/" + part) if cur else part
            if not self.exists(cur):
                try:
                    self.mkdir(cur)
                except SSHDeckError:
                    # A parallel transfer may have created it between the
                    # check and the call; only a still-missing path is fatal.
                    if not self.exists(cur):
                        raise

    def close(self):
        try:
            self._sftp.close()
        except Exception:
            pass
