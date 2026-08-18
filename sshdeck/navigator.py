r"""The navigator: session tree on the left, terminal tabs on the right.

This is the layout the app opens into -- the SecureCRT arrangement, where the
saved sessions are always in view and each connection is a tab beside them
rather than a separate window or a mode you switch into.

Two pieces carry the interaction:

* :class:`SessionTree` -- a filterable tree of saved sessions.  Double-click
  connects, which is the gesture people already have; single-click only
  selects, so browsing the list never opens connections by accident.
* :class:`TabStrip` -- one tab per open terminal, each showing a colour-coded
  state dot, a live ``user@host: cwd`` label taken from the shell's own title
  sequence, and a close button.  Tabs that produce output while in the
  background are marked, so a build finishing on another tab is noticeable
  without watching it.

The widgets own no connection logic: they raise callbacks and let the app
decide.  That keeps them testable and keeps connection handling in one place.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional

#: Compact metrics for the docked session tree.
ROW_HEIGHT = 19
INDENT = 14

# --- tab states ------------------------------------------------------------ #
CONNECTING = "connecting"
CONNECTED = "connected"
ACTIVITY = "activity"
DISCONNECTED = "disconnected"
FAILED = "failed"

#: Colour-coded indicators. The glyph carries the meaning for anyone who
#: cannot distinguish the colours; the colour makes it readable at a glance.
STATE_STYLE: Dict[str, Dict[str, str]] = {
    CONNECTING:   {"glyph": "◌", "colour": "#e9ad0c", "label": "Connecting"},
    CONNECTED:    {"glyph": "●", "colour": "#26a269", "label": "Connected"},
    ACTIVITY:     {"glyph": "◆", "colour": "#2a7bde", "label": "New output"},
    DISCONNECTED: {"glyph": "○", "colour": "#8d8d8d", "label": "Disconnected"},
    FAILED:       {"glyph": "▲", "colour": "#c01c28", "label": "Failed"},
}


def state_style(state: str) -> Dict[str, str]:
    return STATE_STYLE.get(state, STATE_STYLE[DISCONNECTED])


class SessionTree(tk.Frame):
    """Filterable tree of saved sessions; double-click to connect."""

    def __init__(self, master, *,
                 on_connect: Optional[Callable[[str], None]] = None,
                 on_select: Optional[Callable[[str], None]] = None,
                 on_properties: Optional[Callable[[str], None]] = None, **kw):
        super().__init__(master, **kw)
        self._on_connect = on_connect
        self._on_select = on_select
        self._on_properties = on_properties
        self._sessions: List[object] = []

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        self.filter_entry = ttk.Entry(self, textvariable=self.filter_var)
        self.filter_entry.pack(fill="x", padx=4, pady=(3, 2))

        # A session list is scanned, not read: a compact row height and a
        # smaller face fit far more hosts on screen, which is the whole point
        # of having the tree permanently docked.
        # ttk resolves a style's layout from the trailing component, so the
        # name must END with the widget class or the layout is not found.
        self._style_name = f"Compact{id(self)}.Treeview"
        try:
            style = ttk.Style(self)
            base = tkfont.nametofont("TkDefaultFont").copy()
            base.configure(size=max(7, base.cget("size") - 1))
            self._tree_font = base
            style.configure(self._style_name, font=base, rowheight=ROW_HEIGHT,
                            indent=INDENT)
            tree_kw = {"style": self._style_name}
        except Exception:
            tree_kw = {}
        self.tree = ttk.Treeview(self, show="tree", selectmode="browse",
                                 **tree_kw)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0),
                       pady=(0, 3))

        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Return>", self._double_click)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Button-3>", self._context)

    # -- data ------------------------------------------------------------- #
    def set_sessions(self, sessions: List[object]) -> None:
        self._sessions = list(sessions)
        self.refresh()

    def refresh(self) -> None:
        needle = self.filter_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        root = self.tree.insert("", "end", iid="__root__", text="Sessions",
                                open=True)
        folders: Dict[str, str] = {}
        for session in self._sessions:
            name = getattr(session, "name", "")
            host = getattr(session, "host", "")
            if needle and needle not in name.lower() and needle not in host.lower():
                continue
            folder = getattr(session, "folder", "") or ""
            parent = root
            if folder:
                if folder not in folders:
                    folders[folder] = self.tree.insert(
                        root, "end", text=folder, open=True)
                parent = folders[folder]
            self.tree.insert(parent, "end", iid=name, text=name)

    def selected_name(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel or sel[0] in ("__root__",):
            return None
        name = sel[0]
        return None if self.tree.get_children(name) else name

    # -- events ----------------------------------------------------------- #
    def _double_click(self, _event=None) -> str:
        name = self.selected_name()
        if name and self._on_connect:
            self._on_connect(name)
        return "break"

    def _selected(self, _event=None) -> None:
        name = self.selected_name()
        if name and self._on_select:
            self._on_select(name)

    def _context(self, event) -> str:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        name = self.selected_name()
        if not name:
            return "break"
        menu = tk.Menu(self, tearoff=0)
        if self._on_connect:
            menu.add_command(label="Connect",
                             command=lambda: self._on_connect(name))
        if self._on_properties:
            menu.add_command(label="Properties…",
                             command=lambda: self._on_properties(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"


class TabStrip(tk.Frame):
    """One row of terminal tabs with colour-coded state indicators."""

    def __init__(self, master, *,
                 on_select: Optional[Callable[[str], None]] = None,
                 on_close: Optional[Callable[[str], None]] = None, **kw):
        super().__init__(master, **kw)
        self._on_select = on_select
        self._on_close = on_close
        self._tabs: Dict[str, Dict] = {}
        self._order: List[str] = []
        self.active: Optional[str] = None

    # -- api --------------------------------------------------------------- #
    def add(self, tab_id: str, label: str, state: str = CONNECTING) -> None:
        if tab_id in self._tabs:
            return
        holder = tk.Frame(self, bd=1, relief="raised")
        dot = tk.Label(holder, text=state_style(state)["glyph"],
                       fg=state_style(state)["colour"])
        title = tk.Label(holder, text=label)
        close = tk.Label(holder, text="✕", cursor="hand2")
        dot.pack(side="left", padx=(6, 2))
        title.pack(side="left", padx=(0, 6))
        close.pack(side="left", padx=(0, 6))
        holder.pack(side="left", padx=(0, 2))

        for widget in (holder, dot, title):
            widget.bind("<Button-1>", lambda _e, i=tab_id: self.select(i))
        close.bind("<Button-1>", lambda _e, i=tab_id: self._close(i))

        self._tabs[tab_id] = {"frame": holder, "dot": dot, "title": title,
                              "state": state, "label": label}
        self._order.append(tab_id)
        if self.active is None:
            self.select(tab_id)

    def remove(self, tab_id: str) -> None:
        tab = self._tabs.pop(tab_id, None)
        if not tab:
            return
        try:
            tab["frame"].destroy()
        except tk.TclError:
            pass
        if tab_id in self._order:
            self._order.remove(tab_id)
        if self.active == tab_id:
            self.active = None
            if self._order:
                self.select(self._order[-1])

    def select(self, tab_id: str) -> None:
        if tab_id not in self._tabs:
            return
        self.active = tab_id
        # Selecting a tab clears its activity mark -- the user has now seen it.
        if self._tabs[tab_id]["state"] == ACTIVITY:
            self.set_state(tab_id, CONNECTED)
        self._restyle()
        if self._on_select:
            self._on_select(tab_id)

    def set_state(self, tab_id: str, state: str) -> None:
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        tab["state"] = state
        style = state_style(state)
        try:
            tab["dot"].configure(text=style["glyph"], fg=style["colour"])
        except tk.TclError:
            pass

    def mark_activity(self, tab_id: str) -> None:
        """Flag background output.  The active tab is being watched already."""
        if tab_id == self.active:
            return
        if self._tabs.get(tab_id, {}).get("state") == CONNECTED:
            self.set_state(tab_id, ACTIVITY)

    def set_label(self, tab_id: str, label: str) -> None:
        tab = self._tabs.get(tab_id)
        if not tab:
            return
        tab["label"] = label
        try:
            tab["title"].configure(text=self._elide(label))
        except tk.TclError:
            pass

    def state_of(self, tab_id: str) -> Optional[str]:
        tab = self._tabs.get(tab_id)
        return tab["state"] if tab else None

    def label_of(self, tab_id: str) -> Optional[str]:
        tab = self._tabs.get(tab_id)
        return tab["label"] if tab else None

    def ids(self) -> List[str]:
        return list(self._order)

    # -- internals --------------------------------------------------------- #
    @staticmethod
    def _elide(label: str, limit: int = 34) -> str:
        """Keep the tail: the path end identifies a tab better than the host."""
        return label if len(label) <= limit else "…" + label[-(limit - 1):]

    def _restyle(self) -> None:
        for tab_id, tab in self._tabs.items():
            try:
                tab["frame"].configure(
                    relief="sunken" if tab_id == self.active else "raised")
            except tk.TclError:
                pass

    def _close(self, tab_id: str) -> str:
        if self._on_close:
            self._on_close(tab_id)
        else:
            self.remove(tab_id)
        return "break"
