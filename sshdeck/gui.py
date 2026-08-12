#!/usr/bin/env python3
r"""SSHDeck -- a pure-stdlib tkinter GUI on top of the ``sshdeck`` library.

A single main window: a left sidebar of sections (Sessions, Terminal, SFTP, Keys,
Port Forwards) and a main panel that swaps to the selected section.  Every network
operation calls the tested core library (never re-implements SSH logic) and runs
on a background thread so the UI stays responsive; results are marshalled back
with ``self.after`` and reported inline -- a clear status plus, on failure, the
``SSHDeckError`` message (never a raw traceback).

Design goals baked in here:
  * pure standard-library tkinter/ttk -- NO third-party GUI deps.  Dark mode is a
    ttk-style + palette swap mirroring the QuickOpen palette.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a note, returns 0) with no display.
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

# NOTE: tkinter is imported lazily inside main()/build_app so that merely importing
# this module (e.g. during packaging or on a headless CI box) never fails.

APP_NAME = "SSHDeck"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "SSHDeck — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"

# (section_id, label) -- section_id maps to a _panel_<id> method.
SECTIONS = [
    ("sessions", "Sessions"),
    ("terminal", "Terminal"),
    ("sftp", "SFTP"),
    ("keys", "Keys"),
    ("forwards", "Port Forwards"),
]

SECTION_DESCRIPTIONS = {
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

# ---- colour palettes (mirror the QuickOpen palette) -------------------------
PALETTES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "text": "#141820",
        "muted": "#5b6472", "primary": "#2f5fe0", "primary_hi": "#2450c8",
        "entry": "#ffffff", "border": "#d5dae2", "sel": "#2f5fe0",
        "sel_fg": "#ffffff", "trough": "#e2e7ef", "ok": "#1f7a3d",
        "err": "#c0392b", "term_bg": "#0b0e14", "term_fg": "#d7dde8",
    },
    "dark": {
        "bg": "#0f1115", "surface": "#1a1e24", "text": "#f1f3f7",
        "muted": "#9aa4b2", "primary": "#5b86f7", "primary_hi": "#7098ff",
        "entry": "#1a1e24", "border": "#2a2f38", "sel": "#5b86f7",
        "sel_fg": "#0f1115", "trough": "#2a2f38", "ok": "#5bd68a",
        "err": "#ff6b5e", "term_bg": "#05070b", "term_fg": "#d7dde8",
    },
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
# The app (built lazily; tkinter imported only inside build_app/main)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to a live tkinter import.

    Kept inside a function so this module imports cleanly without a display.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    from . import guiconfig
    from . import client as sshclient
    from . import forward as forwardmod
    from . import keys as keysmod
    from . import sessions as sessionsmod
    from . import sftp as sftpmod
    from .errors import SSHDeckError

    FONT = "Segoe UI"
    MONO = ("Consolas", 10) if sys.platform == "win32" else ("Monospace", 10)

    # -- the main window --------------------------------------------------
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("1120x720")
            self.minsize(920, 600)

            self.theme = guiconfig.get_theme()
            self._busy = False
            self._panels = {}
            self._current = None
            self._tracked = []          # (tk_widget, role) for manual re-theming
            self._img_refs = []

            # connection state
            self._client = None         # live paramiko client, or None
            self._session = None        # the connected Session
            self._chan = None           # interactive shell channel
            self._chan_stop = None      # threading.Event for the read loop
            self._forwards = []         # [(spec, stopper)]

            self._set_icon()
            self._build_menu()
            self._build_layout()
            self._apply_theme()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._select_first)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("ssh-deck.ico")
                if ico:
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("ssh-deck.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- theming
        def track(self, widget, role):
            self._tracked.append((widget, role))

        def _pal(self):
            return PALETTES[self.theme]

        def _apply_theme(self):
            p = self._pal()
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            self.configure(bg=p["bg"])
            style.configure(".", background=p["bg"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            font=(FONT, 10))
            style.configure("TFrame", background=p["bg"])
            style.configure("Sidebar.TFrame", background=p["surface"])
            style.configure("Card.TFrame", background=p["surface"])
            style.configure("TLabel", background=p["bg"], foreground=p["text"])
            style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
            style.configure("Header.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 15, "bold"))
            style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"],
                            font=(FONT, 10))
            style.configure("Brand.TLabel", background=p["surface"],
                            foreground=p["text"], font=(FONT, 12, "bold"))
            style.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
            style.configure("Err.TLabel", background=p["bg"], foreground=p["err"])
            style.configure("Status.TLabel", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TButton", background=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], focuscolor=p["surface"],
                            padding=(10, 5))
            style.map("TButton",
                      background=[("active", p["trough"]), ("disabled", p["bg"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Accent.TButton", background=p["primary"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Accent.TButton",
                      background=[("active", p["primary_hi"]),
                                  ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Toggle.TButton", background=p["surface"],
                            foreground=p["text"], padding=(8, 4))
            for name in ("TEntry", "TSpinbox"):
                style.configure(name, fieldbackground=p["entry"], foreground=p["text"],
                                insertcolor=p["text"], bordercolor=p["border"])
            style.configure("TCombobox", fieldbackground=p["entry"],
                            foreground=p["text"], background=p["surface"],
                            arrowcolor=p["text"])
            style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])])
            style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
            style.map("TCheckbutton", background=[("active", p["bg"])])
            style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
            style.map("TRadiobutton", background=[("active", p["bg"])])
            style.configure("TLabelframe", background=p["bg"], foreground=p["text"],
                            bordercolor=p["border"])
            style.configure("TLabelframe.Label", background=p["bg"],
                            foreground=p["muted"])
            style.configure("Treeview", background=p["surface"],
                            fieldbackground=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], rowheight=24)
            style.map("Treeview", background=[("selected", p["primary"])],
                      foreground=[("selected", p["sel_fg"])])
            style.configure("Sidebar.Treeview", background=p["surface"],
                            fieldbackground=p["surface"])
            style.configure("TScrollbar", background=p["surface"],
                            troughcolor=p["bg"], bordercolor=p["border"],
                            arrowcolor=p["text"])
            style.configure("TSeparator", background=p["border"])

            for widget, role in list(self._tracked):
                try:
                    if role == "listbox":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                    elif role == "text":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                    elif role == "term":
                        widget.configure(bg=p["term_bg"], fg=p["term_fg"],
                                         insertbackground=p["term_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                except Exception:
                    pass

        def toggle_theme(self):
            self.theme = "dark" if self.theme == "light" else "light"
            guiconfig.set_theme(self.theme)
            self._apply_theme()
            self._theme_btn.configure(
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")

        # ---- menu
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
            viewm.add_command(label="Toggle dark mode", command=self.toggle_theme)
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=self._about)
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
            self._select("sessions")
            self.connect(session)

        # ---- layout
        def _build_layout(self):
            top = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 8))
            top.pack(fill="x", side="top")
            ttk.Label(top, text="SSHDeck", style="Brand.TLabel").pack(side="left")
            ttk.Label(top, style="Status.TLabel",
                      text="  offline · open source · by QuickOpen").pack(side="left")
            self._theme_btn = ttk.Button(
                top, style="Toggle.TButton", command=self.toggle_theme,
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._theme_btn.pack(side="right")
            self._conn_lbl = ttk.Label(top, style="Status.TLabel",
                                       text="not connected")
            self._conn_lbl.pack(side="right", padx=12)

            body = ttk.Frame(self, style="TFrame")
            body.pack(fill="both", expand=True)

            side = ttk.Frame(body, style="Sidebar.TFrame", width=190)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            self.nav = ttk.Treeview(side, show="tree", selectmode="browse",
                                    style="Sidebar.Treeview")
            self.nav.pack(fill="both", expand=True, padx=6, pady=6)
            self._nav_ids = {}
            for sid, label in SECTIONS:
                iid = self.nav.insert("", "end", text="  " + label)
                self._nav_ids[iid] = sid
            self.nav.bind("<<TreeviewSelect>>", self._on_nav_select)

            main = ttk.Frame(body, style="TFrame", padding=(16, 12))
            main.pack(side="left", fill="both", expand=True)

            head = ttk.Frame(main, style="TFrame")
            head.pack(fill="x")
            self.title_lbl = ttk.Label(head, text="Sessions", style="Header.TLabel")
            self.title_lbl.pack(anchor="w")
            self.desc_lbl = ttk.Label(head, text="", style="Sub.TLabel",
                                      wraplength=760, justify="left")
            self.desc_lbl.pack(anchor="w", pady=(2, 8))
            ttk.Separator(main).pack(fill="x")

            self.container = ttk.Frame(main, style="TFrame")
            self.container.pack(fill="both", expand=True, pady=(10, 8))

            bar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 6))
            bar.pack(fill="x", side="bottom")
            self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel",
                                        width=16, anchor="w")
            self.status_lbl.pack(side="left")
            self.result_lbl = ttk.Label(bar, text="", style="Status.TLabel",
                                        anchor="w", wraplength=820, justify="left")
            self.result_lbl.pack(side="left", fill="x", expand=True, padx=8)

        def _select_first(self):
            for iid in self._nav_ids:
                self.nav.selection_set(iid)
                self.nav.see(iid)
                break

        def _select(self, section_id):
            for iid, sid in self._nav_ids.items():
                if sid == section_id:
                    self.nav.selection_set(iid)
                    return

        def _on_nav_select(self, _e=None):
            sel = self.nav.selection()
            if not sel:
                return
            sid = self._nav_ids.get(sel[0])
            if sid:
                self._show_section(sid)

        def _show_section(self, section_id):
            if self._current is not None:
                self._current.pack_forget()
            panel = self._panels.get(section_id)
            if panel is None:
                panel = ttk.Frame(self.container, style="TFrame")
                builder = getattr(self, "_panel_" + section_id, None)
                if builder:
                    builder(panel)
                else:
                    ttk.Label(panel, text="Not implemented.").pack()
                self._panels[section_id] = panel
                self._apply_theme()
            panel.pack(fill="both", expand=True)
            self._current = panel
            label = dict(SECTIONS).get(section_id, section_id)
            self.title_lbl.configure(text=label)
            self.desc_lbl.configure(text=SECTION_DESCRIPTIONS.get(section_id, ""))
            if hasattr(panel, "_on_show"):
                try:
                    panel._on_show()
                except Exception:
                    pass

        # ---- background operation runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            """Run ``work()`` off the UI thread; call ``on_ok(result)`` back on it.

            Errors are shown inline (SSHDeckError message, or a generic note),
            never as a traceback.  Refuses a second op while one is in flight.
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
            self._clear_result(keep_status=True)

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
                    self._set_status("error", kind="err")
                    self._show_error(err)
                    return
                self._set_status("done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- status/result helpers
        def _set_status(self, text, kind="idle"):
            p = self._pal()
            color = {"working": p["primary"], "ok": p["ok"], "err": p["err"]}.get(
                kind, p["muted"])
            self.status_lbl.configure(text=text, foreground=color)

        def _clear_result(self, keep_status=False):
            self.result_lbl.configure(text="")
            if not keep_status:
                self._set_status("Ready")

        def _show_error(self, message):
            self.result_lbl.configure(text="✕ " + message,
                                      foreground=self._pal()["err"])

        def report_success(self, message):
            self.result_lbl.configure(text="✓ " + message,
                                      foreground=self._pal()["ok"])
            self._set_status("done", kind="ok")

        def _update_conn_label(self):
            if self._client is not None and self._session is not None:
                self._conn_lbl.configure(text=f"connected: {self._session.target()}",
                                         foreground=self._pal()["ok"])
            else:
                self._conn_lbl.configure(text="not connected",
                                         foreground=self._pal()["muted"])

        # ================================================================
        # Connection management
        # ================================================================
        def _prompt_secret(self, prompt, title="SSHDeck"):
            return simpledialog.askstring(title, prompt, show="*", parent=self)

        def connect(self, session):
            if session is None:
                self._show_error("Choose a session first.")
                return
            if self._client is not None:
                self.disconnect()
            password = None
            passphrase = None
            if session.auth == "password":
                password = self._prompt_secret(
                    f"Password for {session.target()}:")
                if password is None:
                    self._set_status("Ready")
                    return
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
                            return

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

            self._bg(work, ok, busy=f"Connecting to {session.host}…")

        def disconnect(self):
            self._stop_shell()
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
        # ---------- Sessions ----------
        def _panel_sessions(self, parent):
            left = ttk.Frame(parent, style="TFrame")
            left.pack(side="left", fill="y", padx=(0, 12))
            ttk.Label(left, text="Saved sessions", style="TLabel").pack(anchor="w")
            box = ttk.Frame(left, style="TFrame")
            box.pack(fill="both", expand=True, pady=4)
            self._sess_list = tk.Listbox(box, height=16, width=26,
                                         activestyle="none", exportselection=False)
            sb = ttk.Scrollbar(box, orient="vertical",
                               command=self._sess_list.yview)
            self._sess_list.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._sess_list.pack(side="left", fill="both", expand=True)
            self.track(self._sess_list, "listbox")
            self._sess_list.bind("<<ListboxSelect>>", lambda e: self._load_selected())
            btns = ttk.Frame(left, style="TFrame")
            btns.pack(fill="x", pady=(4, 0))
            ttk.Button(btns, text="New", command=self._new_session,
                       width=6).pack(side="left")
            ttk.Button(btns, text="Delete", command=self._delete_session,
                       width=7).pack(side="left", padx=4)

            right = ttk.Frame(parent, style="TFrame")
            right.pack(side="left", fill="both", expand=True)

            self._sf = {}   # form vars
            def field(label, key, width=34):
                row = ttk.Frame(right, style="TFrame")
                row.pack(fill="x", pady=3)
                ttk.Label(row, text=label, width=12, anchor="w").pack(side="left")
                var = tk.StringVar()
                self._sf[key] = var
                ent = ttk.Entry(row, textvariable=var, width=width)
                ent.pack(side="left", fill="x", expand=True)
                return row

            field("Name", "name")
            field("Host", "host")
            prow = ttk.Frame(right, style="TFrame")
            prow.pack(fill="x", pady=3)
            ttk.Label(prow, text="Port", width=12, anchor="w").pack(side="left")
            self._sf["port"] = tk.StringVar(value="22")
            ttk.Entry(prow, textvariable=self._sf["port"], width=8).pack(side="left")
            ttk.Label(prow, text="User").pack(side="left", padx=(16, 4))
            self._sf["user"] = tk.StringVar()
            ttk.Entry(prow, textvariable=self._sf["user"]).pack(
                side="left", fill="x", expand=True)

            arow = ttk.Frame(right, style="TFrame")
            arow.pack(fill="x", pady=3)
            ttk.Label(arow, text="Auth", width=12, anchor="w").pack(side="left")
            self._sf["auth"] = tk.StringVar(value="key")
            ttk.Combobox(arow, textvariable=self._sf["auth"], state="readonly",
                         width=12, values=list(sessionsmod.AUTH_METHODS)).pack(
                side="left")

            krow = ttk.Frame(right, style="TFrame")
            krow.pack(fill="x", pady=3)
            ttk.Label(krow, text="Key path", width=12, anchor="w").pack(side="left")
            self._sf["key_path"] = tk.StringVar()
            ttk.Entry(krow, textvariable=self._sf["key_path"]).pack(
                side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Button(krow, text="Browse…", width=9,
                       command=self._browse_key).pack(side="left")

            field("Jump host", "jump")
            ttk.Label(right, style="Muted.TLabel",
                      text="Jump host is optional: user@bastion:22. Passwords and "
                           "key passphrases are never saved — you'll be asked when "
                           "connecting.").pack(anchor="w", pady=(2, 8))

            actions = ttk.Frame(right, style="TFrame")
            actions.pack(fill="x", pady=6)
            ttk.Button(actions, text="Save session",
                       command=self._save_session).pack(side="left")
            ttk.Button(actions, text="Connect", style="Accent.TButton",
                       command=self._connect_form).pack(side="left", padx=6)
            ttk.Button(actions, text="Disconnect",
                       command=self.disconnect).pack(side="left")

            self._refresh_sessions()

        def _refresh_sessions(self):
            try:
                self._sessions_cache = sessionsmod.load_all()
            except SSHDeckError as exc:
                self._sessions_cache = []
                self._show_error(str(exc))
            self._sess_list.delete(0, "end")
            for s in self._sessions_cache:
                self._sess_list.insert("end", f"{s.name}  ({s.target()})")

        def _selected_session(self):
            sel = self._sess_list.curselection()
            if not sel:
                return None
            idx = sel[0]
            if 0 <= idx < len(self._sessions_cache):
                return self._sessions_cache[idx]
            return None

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
            self._select("sessions")
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

        # ---------- Terminal ----------
        def _panel_terminal(self, parent):
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            self._term_btn = ttk.Button(top, text="Open shell",
                                        style="Accent.TButton",
                                        command=self._open_shell)
            self._term_btn.pack(side="left")
            ttk.Button(top, text="Close shell",
                       command=self._stop_shell).pack(side="left", padx=6)
            ttk.Label(top, style="Muted.TLabel",
                      text="  Interactive shell over the current connection.").pack(
                side="left")

            body = ttk.Frame(parent, style="TFrame")
            body.pack(fill="both", expand=True, pady=(8, 0))
            self._term = tk.Text(body, wrap="char", height=22, font=MONO,
                                 state="disabled")
            sb = ttk.Scrollbar(body, orient="vertical", command=self._term.yview)
            self._term.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self._term.pack(side="left", fill="both", expand=True)
            self.track(self._term, "term")
            self._term.bind("<Key>", self._term_key)

        def _term_write(self, text):
            self._term.configure(state="normal")
            self._term.insert("end", text)
            self._term.see("end")
            self._term.configure(state="disabled")

        def _open_shell(self):
            if not self._require_connection():
                return
            if self._chan is not None:
                self._show_error("A shell is already open.")
                return
            try:
                self._chan = sshclient.open_shell(self._client)
            except SSHDeckError as exc:
                self._show_error(str(exc))
                return
            self._chan_stop = threading.Event()
            self._term.configure(state="normal")
            self._term.delete("1.0", "end")
            self._term.configure(state="disabled")

            def reader():
                import time
                while not self._chan_stop.is_set():
                    try:
                        if self._chan.recv_ready():
                            data = self._chan.recv(4096)
                            if not data:
                                break
                            text = data.decode("utf-8", "replace")
                            self.after(0, lambda t=text: self._term_write(t))
                        else:
                            time.sleep(0.03)
                        if self._chan.exit_status_ready() and not self._chan.recv_ready():
                            break
                    except Exception:
                        break
                self.after(0, lambda: self._set_status("shell closed"))

            threading.Thread(target=reader, daemon=True).start()
            self.report_success("Shell open — type below.")
            self._term.focus_set()

        def _term_key(self, event):
            if self._chan is None:
                return "break"
            ch = event.char
            keysym = event.keysym
            try:
                if keysym == "Return":
                    self._chan.send("\n")
                elif keysym == "BackSpace":
                    self._chan.send("\x7f")
                elif keysym == "Tab":
                    self._chan.send("\t")
                elif ch and ch.isprintable():
                    self._chan.send(ch)
            except Exception:
                pass
            return "break"

        def _stop_shell(self):
            if self._chan_stop is not None:
                self._chan_stop.set()
            if self._chan is not None:
                try:
                    self._chan.close()
                except Exception:
                    pass
            self._chan = None
            self._chan_stop = None

        # ---------- SFTP ----------
        def _panel_sftp(self, parent):
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            ttk.Button(top, text="Connect remote pane",
                       command=self._sftp_open).pack(side="left")
            ttk.Label(top, style="Muted.TLabel",
                      text="  Local (left) ⇄ remote (right).").pack(side="left")

            panes = ttk.Frame(parent, style="TFrame")
            panes.pack(fill="both", expand=True, pady=(8, 0))

            # local pane
            lp = ttk.Labelframe(panes, text="Local", padding=6)
            lp.pack(side="left", fill="both", expand=True, padx=(0, 6))
            self._local_path = tk.StringVar(value=os.path.expanduser("~"))
            lpr = ttk.Frame(lp, style="TFrame")
            lpr.pack(fill="x")
            ttk.Entry(lpr, textvariable=self._local_path).pack(
                side="left", fill="x", expand=True, padx=(0, 4))
            ttk.Button(lpr, text="Go", width=4,
                       command=self._local_refresh).pack(side="left")
            self._local_list = tk.Listbox(lp, height=16, activestyle="none",
                                          exportselection=False)
            self._local_list.pack(fill="both", expand=True, pady=4)
            self.track(self._local_list, "listbox")
            self._local_list.bind("<Double-Button-1>",
                                  lambda e: self._local_enter())
            lb = ttk.Frame(lp, style="TFrame")
            lb.pack(fill="x")
            ttk.Button(lb, text="Upload →", style="Accent.TButton",
                       command=self._sftp_upload).pack(side="left")

            # remote pane
            rp = ttk.Labelframe(panes, text="Remote", padding=6)
            rp.pack(side="left", fill="both", expand=True, padx=(6, 0))
            self._remote_path = tk.StringVar(value=".")
            rpr = ttk.Frame(rp, style="TFrame")
            rpr.pack(fill="x")
            ttk.Entry(rpr, textvariable=self._remote_path).pack(
                side="left", fill="x", expand=True, padx=(0, 4))
            ttk.Button(rpr, text="Go", width=4,
                       command=self._remote_refresh).pack(side="left")
            self._remote_list = tk.Listbox(rp, height=16, activestyle="none",
                                           exportselection=False)
            self._remote_list.pack(fill="both", expand=True, pady=4)
            self.track(self._remote_list, "listbox")
            self._remote_list.bind("<Double-Button-1>",
                                   lambda e: self._remote_enter())
            rb = ttk.Frame(rp, style="TFrame")
            rb.pack(fill="x")
            ttk.Button(rb, text="← Download", style="Accent.TButton",
                       command=self._sftp_download).pack(side="left")
            ttk.Button(rb, text="Mkdir", command=self._sftp_mkdir).pack(
                side="left", padx=4)
            ttk.Button(rb, text="Delete", command=self._sftp_delete).pack(
                side="left")

            self._sftp = None
            self._local_entries = []
            self._remote_entries = []
            self._local_refresh()

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
            self._local_list.insert("end", "📁 ..")
            for n in names:
                is_dir = os.path.isdir(os.path.join(path, n))
                self._local_list.insert("end", ("📁 " if is_dir else "   ") + n)

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
                self._remote_list.insert("end", "📁 ..")
                for e in entries:
                    self._remote_list.insert(
                        "end", ("📁 " if e.is_dir else "   ") + e.name)
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
            self._bg(lambda: self._sftp.put(local, remote),
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
            self._bg(lambda: self._sftp.get(remote, local),
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
        def _panel_keys(self, parent):
            gen = ttk.Labelframe(parent, text="Generate a key pair", padding=10)
            gen.pack(fill="x")
            row = ttk.Frame(gen, style="TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text="Type", width=12, anchor="w").pack(side="left")
            self._k_type = tk.StringVar(value="ed25519")
            ttk.Combobox(row, textvariable=self._k_type, state="readonly",
                         width=12, values=list(keysmod.KEY_TYPES)).pack(side="left")
            orow = ttk.Frame(gen, style="TFrame")
            orow.pack(fill="x", pady=3)
            ttk.Label(orow, text="Save to", width=12, anchor="w").pack(side="left")
            self._k_out = tk.StringVar(
                value=os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519"))
            ttk.Entry(orow, textvariable=self._k_out).pack(
                side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Button(orow, text="Browse…", width=9,
                       command=self._browse_out).pack(side="left")
            prow = ttk.Frame(gen, style="TFrame")
            prow.pack(fill="x", pady=3)
            ttk.Label(prow, text="Passphrase", width=12, anchor="w").pack(side="left")
            self._k_pass = tk.StringVar()
            ttk.Entry(prow, textvariable=self._k_pass, show="*").pack(
                side="left", fill="x", expand=True)
            ttk.Label(prow, text="Comment").pack(side="left", padx=(12, 4))
            self._k_comment = tk.StringVar()
            ttk.Entry(prow, textvariable=self._k_comment, width=16).pack(side="left")
            ttk.Button(gen, text="Generate", style="Accent.TButton",
                       command=self._gen_key).pack(anchor="w", pady=(6, 0))

            show = ttk.Labelframe(parent, text="Public key", padding=10)
            show.pack(fill="both", expand=True, pady=(10, 0))
            row2 = ttk.Frame(show, style="TFrame")
            row2.pack(fill="x")
            ttk.Button(row2, text="Load a key…",
                       command=self._load_pub).pack(side="left")
            ttk.Button(row2, text="Copy to clipboard",
                       command=self._copy_pub).pack(side="left", padx=6)
            self._pub = tk.Text(show, height=5, wrap="char", font=MONO)
            self._pub.pack(fill="both", expand=True, pady=(6, 0))
            self.track(self._pub, "text")

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
        def _panel_forwards(self, parent):
            form = ttk.Labelframe(parent, text="Add a forward", padding=10)
            form.pack(fill="x")
            row = ttk.Frame(form, style="TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text="Kind", width=10, anchor="w").pack(side="left")
            self._fw_kind = tk.StringVar(value="local")
            ttk.Combobox(row, textvariable=self._fw_kind, state="readonly",
                         width=10, values=list(forwardmod.KINDS)).pack(side="left")
            ttk.Label(row, text="Spec").pack(side="left", padx=(16, 4))
            self._fw_spec = tk.StringVar(value="8080:localhost:80")
            ttk.Entry(row, textvariable=self._fw_spec).pack(
                side="left", fill="x", expand=True)
            ttk.Label(form, style="Muted.TLabel",
                      text="Spec: [bind_host:]bind_port:dest_host:dest_port  "
                           "(e.g. 8080:localhost:80)").pack(anchor="w", pady=(2, 6))
            fb = ttk.Frame(form, style="TFrame")
            fb.pack(fill="x")
            ttk.Button(fb, text="Describe",
                       command=self._fw_describe).pack(side="left")
            ttk.Button(fb, text="Start forward", style="Accent.TButton",
                       command=self._fw_start).pack(side="left", padx=6)

            active = ttk.Labelframe(parent, text="Active forwards", padding=10)
            active.pack(fill="both", expand=True, pady=(10, 0))
            self._fw_list = tk.Listbox(active, height=10, activestyle="none",
                                       exportselection=False)
            self._fw_list.pack(fill="both", expand=True)
            self.track(self._fw_list, "listbox")
            ttk.Button(active, text="Stop selected",
                       command=self._fw_stop).pack(anchor="w", pady=(6, 0))

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

        # ---- About / shutdown
        def _about(self):
            win = tk.Toplevel(self)
            win.title("About SSHDeck")
            win.configure(bg=self._pal()["bg"])
            win.resizable(False, False)
            frm = ttk.Frame(win, style="TFrame", padding=18)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="SSHDeck", style="Header.TLabel").pack(anchor="w")
            ttk.Label(frm, text=f"Version {APP_VERSION}",
                      style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
            ttk.Label(frm, style="TLabel", justify="left", wraplength=380,
                      text="A fast, fully-offline SSH & SFTP client — saved "
                           "sessions, an interactive shell, a dual-pane file "
                           "browser, key management and port forwards.\n\n"
                           "100% AI-built, open source, published on QuickOpen.\n"
                           "Credentials stay on your machine.").pack(anchor="w")
            ttk.Label(frm, style="Sub.TLabel", justify="left", wraplength=380,
                      text="Licensed under Apache-2.0. Built on paramiko."
                      ).pack(anchor="w", pady=(8, 4))
            link = ttk.Label(frm, text="Project page: quickopen.ai",
                             style="Ok.TLabel", cursor="hand2")
            link.pack(anchor="w", pady=(4, 10))
            link.bind("<Button-1>", lambda e: open_with_default_app(PROJECT_URL))
            ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e")
            win.transient(self)
            win.grab_set()

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
    With no display (e.g. a server), it prints a friendly note and returns 0
    instead of raising.
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
