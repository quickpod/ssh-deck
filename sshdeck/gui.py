#!/usr/bin/env python3
r"""SSHDeck -- an Aura (QuickOpen design system) GUI on top of the ``sshdeck`` library.

A single Aura window: a left sidebar of sections (Sessions, Terminal, SFTP, Keys,
Port Forwards, About) and a main panel that swaps to the selected section.  Every
network operation calls the tested core library (never re-implements SSH logic)
and runs on a background thread so the UI stays responsive; results are marshalled
back with ``self.after`` and reported inline in the Aura status bar -- a clear
status plus, on failure, the ``SSHDeckError`` message (never a raw traceback).

Design goals baked in here (mirrors the QuickOpen house style):
  * built on the vendored ``sshdeck/aura.py`` design system, which layers the
    quickopen.ai look (deep space + light) over CustomTkinter.  Runtime deps:
    ``customtkinter`` (+ ``darkdetect``) — declared in requirements.txt; the
    PyInstaller build adds ``--collect-all customtkinter``.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a note, returns 0) with no display or
    with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Secrets stay in memory: passwords/passphrases are prompted at connect time and
    never persisted; saved sessions store host/user/key-path only.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so that
# merely importing this module (e.g. during packaging or on a headless CI box)
# never fails.

APP_NAME = "SSHDeck"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "SSHDeck — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#2ecf80"      # UI-accent registry override (icon is near-black)

# (section_id, label, glyph) -- section_id maps to a _build_<id> method.
# Glyphs are DejaVu-safe (verified against the font cmap; see aura README §6).
SECTIONS = [
    ("navigator", "Navigator", "▤"),
    ("sessions", "Sessions", "⛁"),
    ("sftp", "SFTP", "⇅"),
    ("keys", "Keys", "⚲"),
    ("forwards", "Port Forwards", "⇄"),
    ("about", "About", "◈"),
]

SECTION_DESCRIPTIONS = {
    "navigator": "Your sessions on the left, terminals on the right. "
                 "Double-click a session to open it in a new tab.",
    "sessions": "Saved connection profiles. Add, edit, connect — passwords and "
                "passphrases are asked for at connect time, never stored.",
    "terminal": "An interactive shell over the current connection.",
    "sftp": "Browse local and remote files side by side; upload, download, "
            "make folders and delete.",
    "keys": "Generate ed25519/RSA key pairs and copy the public key for a "
            "server's authorized_keys.",
    "forwards": "Local and remote port forwards (SSH tunnels) over the current "
                "connection.",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build.

    For a frozen exe we look only at ``sys._MEIPASS`` and the executable's own
    directory (never ``__file__``).  From source we also consult the package dir,
    the repo root and the CWD.  Returns an absolute path or ``None``.
    """
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def human_size(num_bytes):
    """Human-readable byte size."""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def open_with_default_app(path):
    """Open a file/URL with the OS default application, guarded."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports.

    Kept inside a function so this module imports cleanly without a display
    (and without customtkinter installed).
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    import customtkinter as ctk

    from . import aura
    from . import navigator
    from . import termview
    from . import guiconfig
    from . import client as sshclient
    from . import forward as forwardmod
    from . import keys as keysmod
    from . import sessions as sessionsmod
    from . import sftp as sftpmod
    from .errors import SSHDeckError

    MONO = ("Consolas", 10) if sys.platform == "win32" else ("Monospace", 10)

    # -- the main window --------------------------------------------------
    class App(aura.AuraApp):
        def __init__(self):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("ssh-deck.png"), version=APP_VERSION,
                tagline="offline SSH/SFTP",
                on_theme_change=guiconfig.set_theme,
                size=(1120, 720), min_size=(920, 600))

            self._busy = False
            self._img_refs_gui = []      # AuraApp owns self._img_refs — keep ours apart

            # connection state
            self._client = None          # live paramiko client, or None
            self._session = None         # the connected Session
            self._terms = {}             # tab id -> live terminal + channel
            self._term_seq = 0           # monotonic id for new tabs
            self._forwards = []          # [(spec, stopper)]

            self._set_icon()
            self._build_menu()

            # header-right live connection indicator
            self._conn_lbl = ctk.CTkLabel(
                self.header_actions, text="not connected",
                font=aura.font(role="caption"))
            self._conn_lbl.pack(side="right")

            for sid, label, glyph in SECTIONS:
                self.add_section(sid, label, glyph,
                                 getattr(self, "_build_" + sid))
            self.show("navigator")
            self._install_sidebar_toggle()
            # Start collapsed: the navigator is the point of the window and
            # the section list is a place you visit, not somewhere you live.
            self.after(60, lambda: self._set_sidebar(False))
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- collapsible sidebar
        def _install_sidebar_toggle(self):
            """Add a ⯈/⯇ button that folds the section sidebar away.

            Implemented here rather than in aura.py because the design system
            is vendored and re-vendored; a change there would be lost.
            """
            self._sidebar_open = True
            try:
                self._sidebar_full_w = int(self.sidebar.cget("width")) or 248
            except Exception:
                self._sidebar_full_w = 248
            self._sidebar_btn = aura.AuraButton(
                self.header_actions, "⯇", kind="secondary", width=34,
                command=self._toggle_sidebar)
            self._sidebar_btn.pack(side="right", padx=(0, 8))

        def _toggle_sidebar(self):
            self._set_sidebar(not getattr(self, "_sidebar_open", True))

        def _set_sidebar(self, opened):
            """Show or collapse the sidebar, giving the width to the content."""
            self._sidebar_open = bool(opened)
            try:
                if opened:
                    self.sidebar.configure(width=self._sidebar_full_w)
                    self.sidebar.grid()
                    self._sidebar_btn.configure(text="⯇")
                else:
                    # grid_remove keeps the row/column config, so restoring is
                    # a single call and nothing else has to be re-laid out.
                    self.sidebar.grid_remove()
                    self._sidebar_btn.configure(text="⯈")
            except Exception:
                pass
            # The terminal grid depends on available width, so refit it.
            for entry in getattr(self, "_terms", {}).values():
                try:
                    self.after(180, entry["view"].fit)
                except Exception:
                    pass

        # ---- assets / icon (window/taskbar icon; sidebar icon is AuraApp's)
        def _set_icon(self):
            try:
                ico = asset_path("ssh-deck.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("ssh-deck.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs_gui.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- menu (native menus stay; theme lives in the sidebar toggle too)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="New session…", command=self._new_session)
            self._recent_menu = tk.Menu(filem, tearoff=0)
            filem.add_cascade(label="Connect recent", menu=self._recent_menu)
            self._fill_recent_menu()
            filem.add_separator()
            filem.add_command(label="Disconnect", command=self.disconnect)
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=lambda: self.show("about"))
            helpm.add_command(label="Open project page (quickopen.ai)",
                              command=lambda: open_with_default_app(PROJECT_URL))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

        def _fill_recent_menu(self):
            self._recent_menu.delete(0, "end")
            recent = guiconfig.get_recent()
            if not recent:
                self._recent_menu.add_command(label="(none)", state="disabled")
                return
            for name in recent:
                self._recent_menu.add_command(
                    label=name, command=(lambda nn=name: self._connect_named(nn)))

        def _connect_named(self, name):
            try:
                session = sessionsmod.get(name)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self.show("sessions")
            self.connect(session)

        # ---- background operation runner
        def _bg(self, work, on_ok, button=None, busy="Working…",
                on_error=None):
            """Run ``work()`` off the UI thread; call ``on_ok(result)`` back on it.

            Errors are shown inline (SSHDeckError message, or a generic note),
            never as a traceback.  Refuses a second op while one is in flight.
            ``on_error`` lets a caller clean up after a failure -- the
            navigator uses it to mark the tab it already opened as failed
            rather than leaving it stuck on "connecting".
            """
            if self._busy:
                self._show_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self._set_status(busy, kind="working")

            def run():
                try:
                    res, err = work(), None
                except SSHDeckError as ex:
                    res, err = None, str(ex)
                except Exception as ex:      # never leak a traceback
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                if button is not None:
                    try:
                        button.state(["!disabled"])
                    except Exception:
                        pass
                if err is not None:
                    self._show_error(err)
                    if on_error is not None:
                        try:
                            on_error()
                        except Exception:
                            pass
                    return
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- status/result helpers (route to the Aura status bar)
        def _set_status(self, text, kind="idle"):
            self.set_status(text, kind)

        def _show_error(self, message):
            self.set_error(message)

        def report_success(self, message):
            self.set_success(message)

        def _update_conn_label(self):
            if self._client is not None and self._session is not None:
                self._conn_lbl.configure(
                    text=f"● connected: {self._session.target()}")
            else:
                self._conn_lbl.configure(text="not connected")

        # ================================================================
        # Connection management
        # ================================================================
        def _prompt_secret(self, prompt, title="SSHDeck"):
            return simpledialog.askstring(title, prompt, show="*", parent=self)

        def _prompt_credentials(self, session):
            """Ask for whatever *session* needs, or None if the user cancels.

            Returns ``(password, passphrase)``; either may be None when that
            kind of secret is not required. Nothing is stored -- this runs
            per connection, which is why opening a second tab on a
            password session asks again.
            """
            password = passphrase = None
            if session.auth == "password":
                password = self._prompt_secret(
                    f"Password for {session.target()}:")
                if password is None:
                    self._set_status("Ready")
                    return None
            if session.auth == "key" and session.key_path:
                # Try without a passphrase first; prompt only if the key needs one.
                try:
                    keysmod.load_key(session.key_path)
                except SSHDeckError as exc:
                    if "passphrase" in str(exc).lower():
                        passphrase = self._prompt_secret(
                            f"Passphrase for {session.key_path}:")
                        if passphrase is None:
                            self._set_status("Ready")
                            return None
            return password, passphrase

        def connect(self, session, then=None):
            if session is None:
                self._show_error("Choose a session first.")
                return
            if self._client is not None:
                self.disconnect()
            creds = self._prompt_credentials(session)
            if creds is None:
                return
            password, passphrase = creds

            def work():
                return sshclient.connect(session, password=password,
                                         passphrase=passphrase)

            def ok(conn):
                self._client = conn
                self._session = session
                guiconfig.add_recent(session.name)
                self._fill_recent_menu()
                self._update_conn_label()
                self.report_success(f"Connected to {session.target()}.")
                if then is not None:
                    # Runs on the UI thread, after the client is live -- the
                    # navigator uses it to open the tab in one gesture.
                    try:
                        then()
                    except Exception:
                        pass

            self._bg(work, ok, busy=f"Connecting to {session.host}…")

        def disconnect(self):
            # Every tab rides the same client, so dropping it ends them all --
            # closing only the active one would leave dead tabs on screen.
            self._close_all_tabs()
            for _spec, stopper in self._forwards:
                try:
                    stopper()
                except Exception:
                    pass
            self._forwards = []
            if self._client is not None:
                sshclient.close(self._client)
            self._client = None
            self._session = None
            self._update_conn_label()
            self._set_status("Ready")

        def _require_connection(self):
            if self._client is None:
                self._show_error("Connect to a session first (Sessions tab).")
                return False
            return True

        # ================================================================
        # PANELS
        # ================================================================
        @staticmethod
        def _intro(frame, sid):
            text = SECTION_DESCRIPTIONS.get(sid)
            if text:
                aura.Caption(frame, text).pack(anchor="w", pady=(0, 12))

        # ---------- Sessions ----------
        def _build_sessions(self, frame):
            self._intro(frame, "sessions")
            body = ctk.CTkFrame(frame, fg_color="transparent")
            body.pack(fill="both", expand=True)

            left = aura.Card(body, title="Saved sessions")
            left.pack(side="left", fill="y", padx=(0, 14))
            box = ctk.CTkFrame(left.body, fg_color="transparent")
            box.pack(fill="both", expand=True)
            # The navigator tree (sshdeck.navigator) replaces the flat listbox:
            # folders, a filter box, double-click to connect. Same data, same
            # callbacks — the rest of this window does not know the difference.
            self._sess_tree = navigator.SessionTree(
                box,
                on_select=lambda _name: self._load_selected(),
                on_connect=lambda _name: self._connect())
            self._sess_tree.pack(fill="both", expand=True)
            btns = ctk.CTkFrame(left.body, fg_color="transparent")
            btns.pack(fill="x", pady=(10, 0))
            aura.AuraButton(btns, "New", kind="secondary",
                            command=self._new_session, width=64).pack(side="left")
            aura.AuraButton(btns, "Delete", kind="danger",
                            command=self._delete_session, width=72).pack(
                side="left", padx=(8, 0))

            right = aura.Card(body, title="Connection details")
            right.pack(side="left", fill="both", expand=True)
            form = right.body

            self._sf = {}   # form vars

            def field(label, key, width=None):
                row = ctk.CTkFrame(form, fg_color="transparent")
                row.pack(fill="x", pady=4)
                ctk.CTkLabel(row, text=label, width=90, anchor="w",
                             font=aura.font(role="body")).pack(side="left")
                var = tk.StringVar()
                self._sf[key] = var
                kw = {"textvariable": var}
                if width:
                    kw["width"] = width
                aura.AuraEntry(row, **kw).pack(side="left", fill="x", expand=True)
                return row

            field("Name", "name")
            field("Host", "host")

            prow = ctk.CTkFrame(form, fg_color="transparent")
            prow.pack(fill="x", pady=4)
            ctk.CTkLabel(prow, text="Port", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._sf["port"] = tk.StringVar(value="22")
            aura.AuraEntry(prow, textvariable=self._sf["port"], width=80).pack(
                side="left")
            ctk.CTkLabel(prow, text="User", font=aura.font(role="body")).pack(
                side="left", padx=(16, 8))
            self._sf["user"] = tk.StringVar()
            aura.AuraEntry(prow, textvariable=self._sf["user"]).pack(
                side="left", fill="x", expand=True)

            arow = ctk.CTkFrame(form, fg_color="transparent")
            arow.pack(fill="x", pady=4)
            ctk.CTkLabel(arow, text="Auth", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._sf["auth"] = tk.StringVar(value="key")
            aura.AuraCombo(arow, variable=self._sf["auth"], state="readonly",
                           width=140,
                           values=list(sessionsmod.AUTH_METHODS)).pack(side="left")

            krow = ctk.CTkFrame(form, fg_color="transparent")
            krow.pack(fill="x", pady=4)
            ctk.CTkLabel(krow, text="Key path", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._sf["key_path"] = tk.StringVar()
            aura.AuraEntry(krow, textvariable=self._sf["key_path"]).pack(
                side="left", fill="x", expand=True, padx=(0, 8))
            aura.AuraButton(krow, "Browse…", kind="secondary",
                            command=self._browse_key, width=90).pack(side="left")

            field("Jump host", "jump")
            aura.Caption(form,
                         "Jump host is optional: user@bastion:22. Passwords and "
                         "key passphrases are never saved — you'll be asked when "
                         "connecting.").pack(anchor="w", pady=(2, 10))

            actions = ctk.CTkFrame(form, fg_color="transparent")
            actions.pack(fill="x", pady=(4, 0))
            aura.AuraButton(actions, "Save session", kind="secondary",
                            command=self._save_session).pack(side="left")
            aura.AuraButton(actions, "Connect", kind="primary",
                            command=self._connect_form).pack(side="left", padx=8)
            aura.AuraButton(actions, "Disconnect", kind="ghost",
                            command=self.disconnect).pack(side="left")

            self._refresh_sessions()

        def _refresh_sessions(self):
            try:
                self._sessions_cache = sessionsmod.load_all()
            except SSHDeckError as exc:
                self._sessions_cache = []
                self._show_error(str(exc))
            self._sess_tree.set_sessions(self._sessions_cache)

        def _selected_session(self):
            name = self._sess_tree.selected_name()
            if not name:
                return None
            return next((s for s in self._sessions_cache if s.name == name), None)

        def _load_selected(self):
            s = self._selected_session()
            if not s:
                return
            self._sf["name"].set(s.name)
            self._sf["host"].set(s.host)
            self._sf["port"].set(str(s.port))
            self._sf["user"].set(s.user or "")
            self._sf["auth"].set(s.auth)
            self._sf["key_path"].set(s.key_path or "")
            self._sf["jump"].set(s.jump or "")

        def _new_session(self):
            self.show("sessions")
            for key, var in getattr(self, "_sf", {}).items():
                var.set("22" if key == "port" else ("key" if key == "auth" else ""))

        def _browse_key(self):
            p = filedialog.askopenfilename(title="Choose a private key")
            if p:
                self._sf["key_path"].set(p)

        def _form_session(self):
            return sessionsmod.Session(
                name=self._sf["name"].get(), host=self._sf["host"].get(),
                port=self._sf["port"].get() or 22, user=self._sf["user"].get(),
                auth=self._sf["auth"].get(), key_path=self._sf["key_path"].get(),
                jump=self._sf["jump"].get())

        def _save_session(self):
            try:
                session = self._form_session()
                sessionsmod.upsert(session)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self._refresh_sessions()
            self.report_success(f"Saved session {session.name!r}.")

        def _delete_session(self):
            s = self._selected_session()
            if not s:
                self._show_error("Select a session to delete.")
                return
            if not messagebox.askyesno("Delete session",
                                       f"Delete session {s.name!r}?", parent=self):
                return
            try:
                sessionsmod.remove(s.name)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self._refresh_sessions()
            self.report_success(f"Deleted session {s.name!r}.")

        def _connect_form(self):
            try:
                session = self._form_session()
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self.connect(session)

        # ---------- Terminal tabs (owned by the navigator) ----------
        # -- terminal tabs -------------------------------------------------- #
        def _select_tab(self, tab_id):
            """Show one terminal; the others stay live but unpacked."""
            for tid, entry in self._terms.items():
                try:
                    if tid == tab_id:
                        entry["view"].pack(fill="both", expand=True)
                        entry["view"].text.focus_set()
                    else:
                        entry["view"].pack_forget()
                except tk.TclError:
                    pass
            entry = self._terms.get(tab_id)
            if entry:
                # The other sections (SFTP, forwards, keys) act on "the"
                # connection, so the active tab defines which one that is.
                self._client = entry.get("client")
                self._session = entry.get("session")
                self._update_conn_label()
                title = self._tabs.label_of(tab_id) or ""
                try:
                    self.title(f"{title} — {APP_NAME}" if title else APP_NAME)
                except tk.TclError:
                    pass

        def _close_tab(self, tab_id):
            entry = self._terms.pop(tab_id, None)
            if entry:
                stop = entry.get("stop")
                if stop is not None:
                    stop.set()
                try:
                    entry["chan"].close()
                except Exception:
                    pass
                # Each tab owns its connection, so closing the tab closes it.
                client = entry.get("client")
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                    if self._client is client:
                        self._client = None
                        self._session = None
                        self._update_conn_label()
                try:
                    entry["view"].destroy()
                except tk.TclError:
                    pass
            self._tabs.remove(tab_id)
            if self._tabs.active:
                self._select_tab(self._tabs.active)

        def _stop_shell(self):
            if self._tabs.active:
                self._close_tab(self._tabs.active)

        def _close_all_tabs(self):
            for tab_id in list(self._terms):
                self._close_tab(tab_id)

        def _open_shell(self):
            if not self._require_connection():
                return
            try:
                chan = sshclient.open_shell(self._client)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return

            self._term_seq += 1
            tab_id = f"term{self._term_seq}"
            label = self._session_label()
            self._tabs.add(tab_id, label, state=navigator.CONNECTING)

            view = termview.TerminalView(
                self._term_area,
                on_input=lambda data, c=chan: self._send(c, data),
                on_title=lambda t, i=tab_id: self._tab_title(i, t),
                on_activity=lambda i=tab_id: self._tabs.mark_activity(i),
                on_resize=lambda r, c, ch=chan: self._resize_pty(ch, r, c))
            stop = threading.Event()
            self._terms[tab_id] = {"view": view, "chan": chan, "stop": stop}
            self._tabs.set_state(tab_id, navigator.CONNECTED)
            self._tabs.select(tab_id)

            def reader():
                import time
                while not stop.is_set():
                    try:
                        if chan.recv_ready():
                            data = chan.recv(65536)
                            if not data:
                                break
                            text = data.decode("utf-8", "replace")
                            # paramiko's thread must never touch Tk: hand the
                            # chunk to the UI thread and let it parse there.
                            self.after(0, lambda t=text, i=tab_id: self._feed(i, t))
                        else:
                            time.sleep(0.02)
                        if chan.exit_status_ready() and not chan.recv_ready():
                            break
                    except Exception:
                        break
                self.after(0, lambda i=tab_id: self._tab_ended(i))

            threading.Thread(target=reader, daemon=True).start()
            self.report_success("Shell open.")

        def _feed(self, tab_id, text):
            entry = self._terms.get(tab_id)
            if entry:
                entry["view"].feed(text)

        def _resize_pty(self, chan, rows, cols):
            """Tell the remote its terminal changed size."""
            try:
                chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

        def _send(self, chan, data):
            try:
                chan.send(data)
            except Exception:
                pass

        def _tab_title(self, tab_id, title):
            """Live tab label from the shell's own OSC title sequence."""
            if title:
                self._tabs.set_label(tab_id, title)
                if self._tabs.active == tab_id:
                    try:
                        self.title(f"{title} — {APP_NAME}")
                    except tk.TclError:
                        pass

        def _tab_ended(self, tab_id):
            if tab_id in self._terms:
                self._tabs.set_state(tab_id, navigator.DISCONNECTED)

        def _session_label(self):
            name = None
            try:
                name = self._sess_tree.selected_name()
            except Exception:
                pass
            return name or "shell"

        # ---------- Navigator: tree + terminal tabs in one window ----------
        def _build_navigator(self, frame):
            """The primary screen: sessions always in view, terminals beside them.

            The session list and the terminals used to live on separate screens,
            so opening a second host meant navigating away from the first. Here
            they share one window, which is the whole point of the layout.
            """
            split = ctk.CTkFrame(frame, fg_color="transparent")
            split.pack(fill="both", expand=True)

            # -- left dock: the session tree
            left = ctk.CTkFrame(split, width=250)
            left.pack(side="left", fill="y", padx=(0, 10))
            left.pack_propagate(False)
            aura.SectionLabel(left, "SESSIONS").pack(anchor="w", padx=8,
                                                     pady=(8, 2))
            self._nav_tree = navigator.SessionTree(
                left,
                on_connect=self._nav_connect,
                on_select=self._nav_select)
            self._nav_tree.pack(fill="both", expand=True, padx=4, pady=(0, 6))

            navbtns = ctk.CTkFrame(left, fg_color="transparent")
            navbtns.pack(fill="x", padx=6, pady=(0, 8))
            aura.AuraButton(navbtns, "Connect", kind="primary",
                            command=lambda: self._nav_connect(
                                self._nav_tree.selected_name())).pack(
                side="left", padx=(0, 6))
            aura.AuraButton(navbtns, "Edit", kind="secondary",
                            command=lambda: self.show("sessions")).pack(
                side="left")

            # -- right: tabs over the stacked terminals
            right = ctk.CTkFrame(split, fg_color="transparent")
            right.pack(side="left", fill="both", expand=True)
            self._tabs = navigator.TabStrip(
                right, on_select=self._select_tab, on_close=self._close_tab)
            self._tabs.pack(fill="x")
            self._term_area = ctk.CTkFrame(right, fg_color="transparent")
            self._term_area.pack(fill="both", expand=True, pady=(6, 0))

            self._nav_placeholder = aura.Caption(
                self._term_area,
                "Double-click a session on the left to open a terminal.")
            self._nav_placeholder.pack(pady=30)
            self._nav_refresh()

        def _nav_refresh(self):
            try:
                self._nav_tree.set_sessions(sessionsmod.load_all())
            except Exception:
                pass

        def _nav_select(self, name):
            """Selecting only previews; connecting is a deliberate act."""
            self.set_status(f"{name} selected — double-click to connect")

        def _nav_connect(self, name):
            """Open *name* in its own tab, with its own connection.

            Every tab owns a separate client. Reusing whatever connection
            happened to be open meant double-clicking a second host silently
            opened another shell on the *first* one -- the session you asked
            for was ignored.
            """
            if not name:
                return
            try:
                session = sessionsmod.get(name)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            creds = self._prompt_credentials(session)
            if creds is None:
                return
            password, passphrase = creds

            self._term_seq += 1
            tab_id = f"term{self._term_seq}"
            self._tabs.add(tab_id, session.name, state=navigator.CONNECTING)
            self._tabs.select(tab_id)

            def work():
                return sshclient.connect(session, password=password,
                                         passphrase=passphrase)

            def ok(conn):
                self._attach_terminal(tab_id, conn, session)

            def failed():
                self._tabs.set_state(tab_id, navigator.FAILED)

            self._bg(work, ok, busy=f"Connecting to {session.host}…",
                     on_error=failed)

        def _attach_terminal(self, tab_id, conn, session):
            """Open a shell on *conn* and bind it to an existing tab."""
            try:
                chan = sshclient.open_shell(conn)
            except SSHDeckError as exc:
                self._tabs.set_state(tab_id, navigator.FAILED)
                self._show_error(str(exc))
                try:
                    conn.close()
                except Exception:
                    pass
                return

            view = termview.TerminalView(
                self._term_area,
                on_input=lambda data, c=chan: self._send(c, data),
                on_title=lambda t, i=tab_id: self._tab_title(i, t),
                on_activity=lambda i=tab_id: self._tabs.mark_activity(i),
                on_resize=lambda r, c, ch=chan: self._resize_pty(ch, r, c))
            stop = threading.Event()
            self._terms[tab_id] = {"view": view, "chan": chan, "stop": stop,
                                   "client": conn, "session": session}
            self._tabs.set_state(tab_id, navigator.CONNECTED)
            self._tabs.select(tab_id)
            self._start_reader(tab_id, chan, stop)
            guiconfig.add_recent(session.name)
            self.report_success(f"Connected to {session.target()}.")

        def _start_reader(self, tab_id, chan, stop):
            def reader():
                import time
                while not stop.is_set():
                    try:
                        if chan.recv_ready():
                            data = chan.recv(65536)
                            if not data:
                                break
                            text = data.decode("utf-8", "replace")
                            # paramiko's thread must never touch Tk: hand the
                            # chunk over and parse on the UI thread.
                            self.after(0, lambda t=text, i=tab_id: self._feed(i, t))
                        else:
                            time.sleep(0.02)
                        if chan.exit_status_ready() and not chan.recv_ready():
                            break
                    except Exception:
                        break
                self.after(0, lambda i=tab_id: self._tab_ended(i))

            threading.Thread(target=reader, daemon=True).start()

        # ---------- SFTP ----------
        def _build_sftp(self, frame):
            self._intro(frame, "sftp")
            top = ctk.CTkFrame(frame, fg_color="transparent")
            top.pack(fill="x")
            aura.AuraButton(top, "Connect remote pane", kind="secondary",
                            command=self._sftp_open).pack(side="left")
            aura.Caption(top, "Local (left) ⇄ remote (right).").pack(
                side="left", padx=(12, 0))

            panes = ctk.CTkFrame(frame, fg_color="transparent")
            panes.pack(fill="both", expand=True, pady=(12, 0))

            # local pane
            lp = aura.Card(panes, title="Local")
            lp.pack(side="left", fill="both", expand=True, padx=(0, 7))
            self._local_path = tk.StringVar(value=os.path.expanduser("~"))
            lpr = ctk.CTkFrame(lp.body, fg_color="transparent")
            lpr.pack(fill="x")
            aura.AuraEntry(lpr, textvariable=self._local_path).pack(
                side="left", fill="x", expand=True, padx=(0, 6))
            aura.AuraButton(lpr, "Go", kind="secondary", width=48,
                            command=self._local_refresh).pack(side="left")
            self._local_list = tk.Listbox(lp.body, height=16, activestyle="none",
                                          exportselection=False,
                                          font=aura.font(role="body"))
            self._local_list.pack(fill="both", expand=True, pady=8)
            aura.track(self._local_list, "listbox")
            self._local_list.bind("<Double-Button-1>",
                                  lambda e: self._local_enter())
            aura.AuraButton(lp.body, "Upload →", kind="primary",
                            command=self._sftp_upload).pack(anchor="w")

            # remote pane
            rp = aura.Card(panes, title="Remote")
            rp.pack(side="left", fill="both", expand=True, padx=(7, 0))
            self._remote_path = tk.StringVar(value=".")
            rpr = ctk.CTkFrame(rp.body, fg_color="transparent")
            rpr.pack(fill="x")
            aura.AuraEntry(rpr, textvariable=self._remote_path).pack(
                side="left", fill="x", expand=True, padx=(0, 6))
            aura.AuraButton(rpr, "Go", kind="secondary", width=48,
                            command=self._remote_refresh).pack(side="left")
            self._remote_list = tk.Listbox(rp.body, height=16, activestyle="none",
                                           exportselection=False,
                                           font=aura.font(role="body"))
            self._remote_list.pack(fill="both", expand=True, pady=8)
            aura.track(self._remote_list, "listbox")
            self._remote_list.bind("<Double-Button-1>",
                                   lambda e: self._remote_enter())
            rb = ctk.CTkFrame(rp.body, fg_color="transparent")
            rb.pack(fill="x")
            aura.AuraButton(rb, "← Download", kind="primary",
                            command=self._sftp_download).pack(side="left")
            aura.AuraButton(rb, "Mkdir", kind="secondary",
                            command=self._sftp_mkdir).pack(side="left", padx=6)
            aura.AuraButton(rb, "Delete", kind="danger",
                            command=self._sftp_delete).pack(side="left")

            # shared transfer progress (0..1 scale)
            self._xfer_prog = aura.ProgressBar(frame)
            self._xfer_prog.pack(fill="x", pady=(12, 2))
            self._xfer_lbl = aura.Caption(frame, "")
            self._xfer_lbl.pack(anchor="w")

            self._sftp = None
            self._local_entries = []
            self._remote_entries = []
            self._local_refresh()

        def _xfer_progress(self, done, total):
            """paramiko transfer callback(done, total) -> Aura bar (0..1)."""
            def upd():
                self._xfer_prog.set(min(1.0, done / max(1, total)))
                self._xfer_lbl.configure(
                    text=f"{human_size(done)} / {human_size(total)}")
            self.after(0, upd)

        def _local_refresh(self):
            path = self._local_path.get() or os.path.expanduser("~")
            try:
                names = sorted(os.listdir(path),
                               key=lambda n: (not os.path.isdir(
                                   os.path.join(path, n)), n.lower()))
            except Exception as exc:
                self._show_error(f"Cannot list {path}: {exc}")
                return
            self._local_entries = [".."] + names
            self._local_list.delete(0, "end")
            self._local_list.insert("end", "▸ ..")
            for n in names:
                is_dir = os.path.isdir(os.path.join(path, n))
                self._local_list.insert("end", ("▸ " if is_dir else "   ") + n)

        def _local_enter(self):
            sel = self._local_list.curselection()
            if not sel:
                return
            name = self._local_entries[sel[0]]
            base = self._local_path.get()
            target = os.path.abspath(os.path.join(base, name))
            if os.path.isdir(target):
                self._local_path.set(target)
                self._local_refresh()

        def _selected_local(self):
            sel = self._local_list.curselection()
            if not sel or sel[0] == 0:
                return None
            return os.path.join(self._local_path.get(),
                                self._local_entries[sel[0]])

        def _sftp_open(self):
            if not self._require_connection():
                return
            try:
                self._sftp = sftpmod.open_sftp(self._client)
                self._remote_path.set(self._sftp.getcwd())
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self._remote_refresh()

        def _remote_refresh(self):
            if self._sftp is None:
                self._show_error("Open the remote pane first.")
                return
            path = self._remote_path.get() or "."
            def work():
                return self._sftp.listdir(path)
            def ok(entries):
                self._remote_entries = [".."] + entries
                self._remote_list.delete(0, "end")
                self._remote_list.insert("end", "▸ ..")
                for e in entries:
                    self._remote_list.insert(
                        "end", ("▸ " if e.is_dir else "   ") + e.name)
                self.report_success(f"{len(entries)} item(s) in {path}")
            self._bg(work, ok, busy="Listing…")

        def _remote_enter(self):
            sel = self._remote_list.curselection()
            if not sel:
                return
            idx = sel[0]
            base = self._remote_path.get()
            if idx == 0:
                target = os.path.dirname(base.rstrip("/")) or "/"
            else:
                e = self._remote_entries[idx]
                if not e.is_dir:
                    return
                target = base.rstrip("/") + "/" + e.name
            self._remote_path.set(target)
            self._remote_refresh()

        def _selected_remote(self):
            sel = self._remote_list.curselection()
            if not sel or sel[0] == 0:
                return None
            return self._remote_entries[sel[0]]

        def _sftp_upload(self):
            if self._sftp is None:
                self._show_error("Open the remote pane first.")
                return
            local = self._selected_local()
            if not local or not os.path.isfile(local):
                self._show_error("Select a local file to upload.")
                return
            remote = self._remote_path.get().rstrip("/") + "/" + os.path.basename(local)
            self._xfer_prog.set(0)
            self._bg(lambda: self._sftp.put(local, remote,
                                            callback=self._xfer_progress),
                     lambda r: (self._remote_refresh(),
                                self.report_success(f"Uploaded → {remote}")),
                     busy="Uploading…")

        def _sftp_download(self):
            if self._sftp is None:
                self._show_error("Open the remote pane first.")
                return
            e = self._selected_remote()
            if not e or e.is_dir:
                self._show_error("Select a remote file to download.")
                return
            remote = self._remote_path.get().rstrip("/") + "/" + e.name
            local = os.path.join(self._local_path.get(), e.name)
            self._xfer_prog.set(0)
            self._bg(lambda: self._sftp.get(remote, local,
                                            callback=self._xfer_progress),
                     lambda r: (self._local_refresh(),
                                self.report_success(f"Downloaded → {local}")),
                     busy="Downloading…")

        def _sftp_mkdir(self):
            if self._sftp is None:
                self._show_error("Open the remote pane first.")
                return
            name = simpledialog.askstring("New folder", "Folder name:", parent=self)
            if not name:
                return
            remote = self._remote_path.get().rstrip("/") + "/" + name
            self._bg(lambda: self._sftp.mkdir(remote),
                     lambda r: (self._remote_refresh(),
                                self.report_success(f"Created {remote}")),
                     busy="Creating…")

        def _sftp_delete(self):
            if self._sftp is None:
                self._show_error("Open the remote pane first.")
                return
            e = self._selected_remote()
            if not e:
                self._show_error("Select a remote item to delete.")
                return
            remote = self._remote_path.get().rstrip("/") + "/" + e.name
            if not messagebox.askyesno("Delete", f"Delete {remote}?", parent=self):
                return
            self._bg(lambda: self._sftp.remove(remote),
                     lambda r: (self._remote_refresh(),
                                self.report_success(f"Deleted {remote}")),
                     busy="Deleting…")

        # ---------- Keys ----------
        def _build_keys(self, frame):
            gen = aura.Card(frame, title="Generate a key pair")
            gen.pack(fill="x")
            g = gen.body
            row = ctk.CTkFrame(g, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text="Type", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._k_type = tk.StringVar(value="ed25519")
            aura.AuraCombo(row, variable=self._k_type, state="readonly",
                           width=140, values=list(keysmod.KEY_TYPES)).pack(
                side="left")
            orow = ctk.CTkFrame(g, fg_color="transparent")
            orow.pack(fill="x", pady=4)
            ctk.CTkLabel(orow, text="Save to", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._k_out = tk.StringVar(
                value=os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519"))
            aura.AuraEntry(orow, textvariable=self._k_out).pack(
                side="left", fill="x", expand=True, padx=(0, 8))
            aura.AuraButton(orow, "Browse…", kind="secondary", width=90,
                            command=self._browse_out).pack(side="left")
            prow = ctk.CTkFrame(g, fg_color="transparent")
            prow.pack(fill="x", pady=4)
            ctk.CTkLabel(prow, text="Passphrase", width=90, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._k_pass = tk.StringVar()
            aura.AuraEntry(prow, textvariable=self._k_pass, show="*").pack(
                side="left", fill="x", expand=True)
            ctk.CTkLabel(prow, text="Comment", font=aura.font(role="body")).pack(
                side="left", padx=(12, 8))
            self._k_comment = tk.StringVar()
            aura.AuraEntry(prow, textvariable=self._k_comment, width=150).pack(
                side="left")
            aura.AuraButton(g, "Generate", kind="primary",
                            command=self._gen_key).pack(anchor="w", pady=(8, 0))

            show = aura.Card(frame, title="Public key")
            show.pack(fill="both", expand=True, pady=(14, 0))
            row2 = ctk.CTkFrame(show.body, fg_color="transparent")
            row2.pack(fill="x")
            aura.AuraButton(row2, "Load a key…", kind="secondary",
                            command=self._load_pub).pack(side="left")
            aura.AuraButton(row2, "Copy to clipboard", kind="secondary",
                            command=self._copy_pub).pack(side="left", padx=6)
            self._pub = tk.Text(show.body, height=5, wrap="char", font=MONO,
                                relief="flat", padx=8, pady=6)
            self._pub.pack(fill="both", expand=True, pady=(8, 0))
            aura.track(self._pub, "text")

        def _browse_out(self):
            p = filedialog.asksaveasfilename(title="Save key as")
            if p:
                self._k_out.set(p)

        def _gen_key(self):
            out = self._k_out.get().strip()
            ktype = self._k_type.get()
            if not out:
                self._show_error("Choose an output path.")
                return
            if os.path.exists(out) and not messagebox.askyesno(
                    "Overwrite", f"{out} exists. Overwrite?", parent=self):
                return
            def work():
                keysmod.generate_keypair(
                    type=ktype, path=out,
                    passphrase=self._k_pass.get() or None,
                    comment=self._k_comment.get() or None)
                return keysmod.public_key_string(out + ".pub")
            def ok(pub):
                self._pub.delete("1.0", "end")
                self._pub.insert("1.0", pub)
                self.report_success(f"Wrote {out} and {out}.pub")
            self._bg(work, ok, busy="Generating…")

        def _load_pub(self):
            p = filedialog.askopenfilename(title="Choose a key (private or .pub)")
            if not p:
                return
            passphrase = None
            def attempt(pw):
                def work():
                    return keysmod.public_key_string(p, passphrase=pw)
                def ok(pub):
                    self._pub.delete("1.0", "end")
                    self._pub.insert("1.0", pub)
                    self.report_success(f"Public key for {os.path.basename(p)}")
                self._bg(work, ok, busy="Reading…")
            # Try without passphrase; if it's encrypted, prompt once.
            try:
                pub = keysmod.public_key_string(p)
                self._pub.delete("1.0", "end")
                self._pub.insert("1.0", pub)
                self.report_success(f"Public key for {os.path.basename(p)}")
            except SSHDeckError as exc:
                if "passphrase" in str(exc).lower() or "encrypted" in str(exc).lower():
                    pw = self._prompt_secret(f"Passphrase for {os.path.basename(p)}:")
                    if pw is None:
                        return
                    attempt(pw)
                else:
                    self._show_error(str(exc))

        def _copy_pub(self):
            text = self._pub.get("1.0", "end").strip()
            if not text:
                self._show_error("Nothing to copy — generate or load a key first.")
                return
            self.clipboard_clear()
            self.clipboard_append(text)
            self.report_success("Public key copied to clipboard.")

        # ---------- Port Forwards ----------
        def _build_forwards(self, frame):
            self._intro(frame, "forwards")
            form = aura.Card(frame, title="Add a forward")
            form.pack(fill="x")
            f = form.body
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(row, text="Kind", width=70, anchor="w",
                         font=aura.font(role="body")).pack(side="left")
            self._fw_kind = tk.StringVar(value="local")
            aura.AuraCombo(row, variable=self._fw_kind, state="readonly",
                           width=120, values=list(forwardmod.KINDS)).pack(
                side="left")
            ctk.CTkLabel(row, text="Spec", font=aura.font(role="body")).pack(
                side="left", padx=(16, 8))
            self._fw_spec = tk.StringVar(value="8080:localhost:80")
            aura.AuraEntry(row, textvariable=self._fw_spec).pack(
                side="left", fill="x", expand=True)
            aura.Caption(f,
                         "Spec: [bind_host:]bind_port:dest_host:dest_port  "
                         "(e.g. 8080:localhost:80)").pack(anchor="w", pady=(4, 8))
            fb = ctk.CTkFrame(f, fg_color="transparent")
            fb.pack(fill="x")
            aura.AuraButton(fb, "Describe", kind="secondary",
                            command=self._fw_describe).pack(side="left")
            aura.AuraButton(fb, "Start forward", kind="primary",
                            command=self._fw_start).pack(side="left", padx=6)

            active = aura.Card(frame, title="Active forwards")
            active.pack(fill="both", expand=True, pady=(14, 0))
            self._fw_list = tk.Listbox(active.body, height=10, activestyle="none",
                                       exportselection=False,
                                       font=aura.font(role="body"))
            self._fw_list.pack(fill="both", expand=True)
            aura.track(self._fw_list, "listbox")
            aura.AuraButton(active.body, "Stop selected", kind="secondary",
                            command=self._fw_stop).pack(anchor="w", pady=(10, 0))

        def _fw_describe(self):
            try:
                text = forwardmod.describe(self._fw_spec.get(),
                                           kind=self._fw_kind.get())
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self.report_success(text)

        def _fw_start(self):
            if not self._require_connection():
                return
            try:
                spec = forwardmod.parse_forward(self._fw_spec.get(),
                                                kind=self._fw_kind.get())
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            def work():
                return forwardmod.start_forward(self._client, spec)
            def ok(stopper):
                self._forwards.append((spec, stopper))
                self._fw_list.insert("end", spec.describe())
                self.report_success(f"Forward started: {spec}")
            self._bg(work, ok, busy="Starting forward…")

        def _fw_stop(self):
            sel = self._fw_list.curselection()
            if not sel:
                self._show_error("Select an active forward to stop.")
                return
            idx = sel[0]
            if 0 <= idx < len(self._forwards):
                _spec, stopper = self._forwards.pop(idx)
                try:
                    stopper()
                except Exception:
                    pass
                self._fw_list.delete(idx)
                self.report_success("Forward stopped.")

        # ---------- About ----------
        def _build_about(self, frame):
            card = aura.Card(frame, title="About SSHDeck")
            card.pack(fill="x")
            aura.Heading(card.body, APP_NAME).pack(anchor="w")
            aura.Caption(card.body, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                card.body, font=aura.font(), justify="left", anchor="w",
                wraplength=560,
                text="A fast, fully-offline SSH & SFTP client — saved sessions, "
                     "an interactive shell, a dual-pane file browser, key "
                     "management and port forwards.\n\n"
                     "100% AI-built, open source, published on QuickOpen. "
                     "Credentials stay on your machine.").pack(anchor="w")
            aura.Caption(card.body,
                         "Licensed under Apache-2.0. Built on paramiko (LGPL) "
                         "and CustomTkinter (MIT).").pack(anchor="w", pady=(10, 4))
            aura.AuraButton(card.body, "Project page: quickopen.ai", kind="ghost",
                            command=lambda: open_with_default_app(
                                PROJECT_URL)).pack(anchor="w", pady=(6, 0))

        # ---- shutdown
        def _on_close(self):
            try:
                self.disconnect()
            except Exception:
                pass
            self.destroy()

    return App


def main():
    """Entry point: build the root window and run.  Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising.
    """
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    # Headless guard: no display -> return 0 without building anything.
    if sys.platform != "win32" and not os.environ.get("DISPLAY") \
            and not os.environ.get("WAYLAND_DISPLAY"):
        print(f"{APP_NAME}: no graphical display available — the GUI is for the "
              f"desktop. (Use `python -m sshdeck --help` for the CLI.)")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}).")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
