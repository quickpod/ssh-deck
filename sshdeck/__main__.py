r"""Command-line interface: ``python -m sshdeck <command> ...``.

Commands (all print clean output; any :class:`SSHDeckError` exits non-zero with a
one-line ``error: ...`` message and no traceback)::

    sshdeck session list
    sshdeck session add web example.com --user deploy --auth key --key ~/.ssh/id_ed25519
    sshdeck session remove web
    sshdeck run web "uptime"
    sshdeck sftp web ls /var/log
    sshdeck sftp web get /etc/hostname ./hostname
    sshdeck sftp web put ./local.txt /tmp/remote.txt
    sshdeck keygen --type ed25519 --out ~/.ssh/id_ed25519
    sshdeck pubkey ~/.ssh/id_ed25519

Passwords / passphrases are never taken from the command line -- when a session
needs one, the CLI prompts for it (hidden) at connect time.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from . import client as sshclient
from . import forward as forwardmod
from . import keys as keysmod
from . import sessions as sessionsmod
from . import sftp as sftpmod
from .errors import SSHDeckError


# --- helpers ----------------------------------------------------------------
def _human_size(num):
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def _connect_interactive(session):
    """Connect *session*, prompting (hidden) for a password/passphrase if needed."""
    password = None
    passphrase = None
    if session.auth == "password":
        password = getpass.getpass(f"Password for {session.target()}: ")
    try:
        return sshclient.connect(session, password=password, passphrase=passphrase)
    except SSHDeckError as exc:
        # A key may be passphrase-protected -- offer one retry with a passphrase.
        if session.auth == "key" and "passphrase" in str(exc).lower():
            passphrase = getpass.getpass(f"Passphrase for {session.key_path}: ")
            return sshclient.connect(session, passphrase=passphrase)
        raise


# --- command handlers -------------------------------------------------------
def cmd_session_list(a):
    sessions = sessionsmod.load_all()
    if not sessions:
        print("No saved sessions. Add one with: sshdeck session add <name> <host>")
        return
    width = max(len(s.name) for s in sessions)
    for s in sessions:
        jump = f"  (via {s.jump})" if s.jump else ""
        key = f"  key={s.key_path}" if s.key_path else ""
        print(f"{s.name:<{width}}  {s.target():<28}  auth={s.auth}{key}{jump}")


def cmd_session_add(a):
    session = sessionsmod.Session(
        name=a.name, host=a.host, port=a.port, user=a.user,
        auth=a.auth, key_path=a.key, jump=a.jump)
    if a.force:
        sessionsmod.upsert(session)
    else:
        sessionsmod.add(session)
    print(f"Saved session {session.name!r} -> {session.target()} (auth={session.auth})")


def cmd_session_remove(a):
    sessionsmod.remove(a.name)
    print(f"Removed session {a.name!r}")


def cmd_run(a):
    session = sessionsmod.get(a.session)
    conn = _connect_interactive(session)
    try:
        rc, out, err = sshclient.run(conn, a.command)
    finally:
        sshclient.close(conn)
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if err:
        sys.stderr.write(err if err.endswith("\n") else err + "\n")
    return rc


def cmd_sftp(a):
    session = sessionsmod.get(a.session)
    conn = _connect_interactive(session)
    try:
        sftp = sftpmod.open_sftp(conn)
        try:
            if a.op == "ls":
                path = a.paths[0] if a.paths else "."
                for e in sftp.listdir(path):
                    tag = "d" if e.is_dir else "-"
                    size = "" if e.is_dir else _human_size(e.size)
                    print(f"{tag} {size:>8}  {e.name}")
            elif a.op == "get":
                if len(a.paths) != 2:
                    raise SSHDeckError("get needs REMOTE and LOCAL paths")
                sftp.get(a.paths[0], a.paths[1])
                print(f"Downloaded {a.paths[0]} -> {a.paths[1]}")
            elif a.op == "put":
                if len(a.paths) != 2:
                    raise SSHDeckError("put needs LOCAL and REMOTE paths")
                sftp.put(a.paths[0], a.paths[1])
                print(f"Uploaded {a.paths[0]} -> {a.paths[1]}")
            elif a.op == "mkdir":
                if not a.paths:
                    raise SSHDeckError("mkdir needs a PATH")
                sftp.mkdir(a.paths[0])
                print(f"Created {a.paths[0]}")
            elif a.op == "rm":
                if not a.paths:
                    raise SSHDeckError("rm needs a PATH")
                sftp.remove(a.paths[0])
                print(f"Removed {a.paths[0]}")
            else:  # pragma: no cover - argparse restricts choices
                raise SSHDeckError(f"unknown sftp op {a.op!r}")
        finally:
            sftp.close()
    finally:
        sshclient.close(conn)


def cmd_keygen(a):
    path = keysmod.generate_keypair(type=a.type, path=a.out,
                                    passphrase=a.passphrase or None,
                                    bits=a.bits, comment=a.comment)
    print(f"Wrote {a.type} private key -> {path}")
    print(f"Wrote public key       -> {path}.pub")
    print(keysmod.public_key_string(path + ".pub"))


def cmd_pubkey(a):
    print(keysmod.public_key_string(a.path, passphrase=a.passphrase or None,
                                    comment=a.comment))


def cmd_forward(a):
    fwd = forwardmod.parse_forward(a.spec, kind=a.kind)
    print(fwd.describe())


def cmd_import_securecrt(a):
    """Import saved sessions from a SecureCRT configuration (read-only)."""
    from . import guiconfig, scrt

    config_dir = a.source or scrt.default_config_dir()
    found = scrt.discover(config_dir)
    if not found:
        print("No SecureCRT sessions found.")
        return 0

    # SecureCRT's global public key becomes SSHDeck's inherited identity, so
    # the sessions that relied on it keep working untouched.
    global_key = scrt.global_identity_key(config_dir) if config_dir else None
    if global_key and not a.dry_run:
        guiconfig.set_global_key(global_key)
    if global_key:
        import os as _os
        state = "" if _os.path.isfile(global_key) else "  (file not found!)"
        verb = "would set" if a.dry_run else "set"
        print(f"  {verb:14} global identity key -> {global_key}{state}")

    existing = {s.name for s in sessionsmod.load_all()}
    added = skipped = 0
    for item in found:
        clash = item.session.name in existing
        if clash and not a.force:
            skipped += 1
            mark = "skip (exists)"
        elif a.dry_run:
            mark = "would add" if not clash else "would replace"
        else:
            sessionsmod.upsert(item.session)
            existing.add(item.session.name)
            added += 1
            mark = "added" if not clash else "replaced"
        folder = f"[{item.folder}] " if item.folder else ""
        pw = "  (needs password)" if item.needs_password else ""
        print(f"  {mark:14} {folder}{item.session.name} -> "
              f"{item.session.user or '?'}@{item.session.host}:"
              f"{item.session.port}{pw}")

    if global_key:
        import os as _os
        from . import identity as _identity
        if _os.path.isfile(global_key) and _identity.is_encrypted(global_key):
            print("\nThe global key is passphrase-protected. SSHDeck asks for "
                  "it once per run and reuses it for every session; it is "
                  "never written to disk.")

    need = [i.session.name for i in found if i.needs_password]
    print(f"\n{len(found)} found, {added} imported, {skipped} skipped.")
    if a.dry_run:
        print("Dry run — nothing was written. Re-run without --dry-run to import.")
    if need:
        print("\nSecureCRT stores passwords encrypted under its configuration "
              "passphrase, so they could not be read. Re-enter one for:")
        for name in need:
            print(f"  - {name}")
    return 0


# --- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="sshdeck",
        description="Offline SSH/SFTP client. Secrets are prompted at connect "
                    "time, never stored or taken from the command line.")
    sub = p.add_subparsers(dest="command", required=True)

    # session ...
    sp = sub.add_parser("session", help="Manage saved session profiles")
    ssub = sp.add_subparsers(dest="session_cmd", required=True)

    sl = ssub.add_parser("list", help="List saved sessions")
    sl.set_defaults(func=cmd_session_list)

    sa = ssub.add_parser("add", help="Add (or --force replace) a session")
    sa.add_argument("name")
    sa.add_argument("host")
    sa.add_argument("--port", type=int, default=22)
    sa.add_argument("--user")
    sa.add_argument("--auth", choices=sessionsmod.AUTH_METHODS, default="key")
    sa.add_argument("--key", help="private-key path (for key auth)")
    sa.add_argument("--jump", help="jump host, e.g. user@bastion:22")
    sa.add_argument("--force", action="store_true", help="overwrite if it exists")
    sa.set_defaults(func=cmd_session_add)

    sr = ssub.add_parser("remove", help="Remove a session")
    sr.add_argument("name")
    sr.set_defaults(func=cmd_session_remove)

    # run
    r = sub.add_parser("run", help="Run a command on a session")
    r.add_argument("session")
    r.add_argument("command")
    r.set_defaults(func=cmd_run)

    # sftp
    f = sub.add_parser("sftp", help="Transfer/list files over SFTP")
    f.add_argument("session")
    f.add_argument("op", choices=["ls", "get", "put", "mkdir", "rm"])
    f.add_argument("paths", nargs="*")
    f.set_defaults(func=cmd_sftp)

    # import-securecrt
    ic = sub.add_parser("import-securecrt",
                        help="Import saved sessions from SecureCRT (read-only)")
    ic.add_argument("--source", help="SecureCRT Config folder (autodetected "
                                     "when omitted)")
    ic.add_argument("--dry-run", action="store_true",
                    help="show what would be imported, write nothing")
    ic.add_argument("--force", action="store_true",
                    help="replace sessions whose name already exists")
    ic.set_defaults(func=cmd_import_securecrt)

    # keygen
    k = sub.add_parser("keygen", help="Generate an SSH key pair")
    k.add_argument("--type", choices=keysmod.KEY_TYPES, default="ed25519")
    k.add_argument("--out", required=True, help="private-key output path")
    k.add_argument("--bits", type=int, help="RSA key size (default 3072)")
    k.add_argument("--passphrase", help="encrypt the private key (optional)")
    k.add_argument("--comment", help="comment for the public key")
    k.set_defaults(func=cmd_keygen)

    # pubkey
    pk = sub.add_parser("pubkey", help="Print the public key for a key file")
    pk.add_argument("path")
    pk.add_argument("--passphrase")
    pk.add_argument("--comment")
    pk.set_defaults(func=cmd_pubkey)

    # forward (describe a spec)
    fw = sub.add_parser("forward", help="Describe a port-forward spec")
    fw.add_argument("spec", help="e.g. 8080:localhost:80")
    fw.add_argument("--kind", choices=forwardmod.KINDS, default="local")
    fw.set_defaults(func=cmd_forward)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except SSHDeckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\naborted", file=sys.stderr)
        return 130
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
