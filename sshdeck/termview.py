r"""The terminal widget: renders a :class:`~sshdeck.screen.Screen` into Tk.

Kept apart from :mod:`sshdeck.gui` so the emulation and the rendering can be
reasoned about (and changed) separately, and so the parsing/buffer layers stay
importable without a display.

What it does beyond drawing characters:

* **Styles become tags.**  Each distinct colour/attribute combination gets one
  Tk tag, created on demand and reused, so a screen full of ``ls`` colour does
  not create thousands of tags.
* **Copy on select, right-click paste.**  Selecting text puts it on the
  clipboard immediately and a right-click sends the clipboard to the shell --
  the behaviour terminal users have muscle memory for, and the reason
  Ctrl+C/Ctrl+V cannot be used here (Ctrl+C must reach the remote program).
* **Redraw is coalesced.**  Output arrives in small bursts; repainting per
  chunk makes a busy screen crawl. Repaints are scheduled on the Tk loop and
  collapsed, so a flood costs one redraw per frame rather than one per read.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from .ansi import DEFAULT_BG, DEFAULT_FG, Style
from .screen import Screen

DEFAULT_FONT = ("DejaVu Sans Mono", 11)
REDRAW_MS = 16                     # ~60fps ceiling on repaints


class TerminalView(tk.Frame):
    """A read-mostly terminal display bound to one :class:`Screen`."""

    def __init__(self, master, *, rows: int = 24, cols: int = 80,
                 font=DEFAULT_FONT,
                 on_input: Optional[Callable[[str], None]] = None,
                 on_title: Optional[Callable[[str], None]] = None,
                 on_activity: Optional[Callable[[], None]] = None, **kw):
        super().__init__(master, **kw)
        self.screen = Screen(rows=rows, cols=cols)
        self._on_input = on_input
        self._on_title = on_title
        self._on_activity = on_activity
        self._tags: set = set()
        self._redraw_job = None
        self._last_title = ""

        self.text = tk.Text(self, wrap="none", font=font, relief="flat",
                            background=DEFAULT_BG, foreground=DEFAULT_FG,
                            insertbackground=DEFAULT_FG,
                            highlightthickness=0, padx=6, pady=4,
                            state="disabled", cursor="xterm",
                            selectbackground="#3465a4", selectforeground="#ffffff")
        self.scroll = tk.Scrollbar(self, orient="vertical",
                                   command=self.text.yview)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.bind("<Key>", self._on_key)
        self.text.bind("<<Selection>>", self._copy_selection)
        # Button-3 on X11 / Button-2 on some setups: paste into the shell.
        self.text.bind("<Button-3>", self._paste)
        self.text.bind("<Button-2>", self._paste)
        self.text.bind("<Button-1>", lambda e: self.text.focus_set(), add="+")

    # -- output ------------------------------------------------------------ #
    def feed(self, data: str) -> None:
        """Push shell output into the buffer and schedule a repaint."""
        self.screen.feed(data)
        if self._on_title and self.screen.title != self._last_title:
            self._last_title = self.screen.title
            try:
                self._on_title(self.screen.title)
            except Exception:
                pass
        if self._on_activity:
            try:
                self._on_activity()
            except Exception:
                pass
        self._schedule_redraw()

    def clear(self) -> None:
        self.screen = Screen(rows=self.screen.rows, cols=self.screen.cols)
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        if self._redraw_job is not None:
            return
        try:
            self._redraw_job = self.after(REDRAW_MS, self._redraw)
        except Exception:
            self._redraw_job = None

    def _tag_for(self, style: Style) -> str:
        name = style.key()
        if name not in self._tags:
            fg, bg = style.resolved()
            cfg = {"foreground": fg, "background": bg}
            if style.underline:
                cfg["underline"] = True
            if style.strike:
                cfg["overstrike"] = True
            self.text.tag_configure(name, **cfg)
            self._tags.add(name)
        return name

    def _redraw(self) -> None:
        self._redraw_job = None
        widget = self.text
        try:
            at_bottom = widget.yview()[1] >= 0.999
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            for row in self.screen.all_rows():
                # Coalesce equal-styled runs; one insert per run, not per cell.
                start = 0
                while start < len(row):
                    style = row[start][1]
                    end = start + 1
                    while end < len(row) and row[end][1] == style:
                        end += 1
                    chunk = "".join(ch for ch, _ in row[start:end])
                    if chunk.strip() or style.bg is not None:
                        widget.insert("end", chunk, self._tag_for(style))
                    else:
                        widget.insert("end", chunk)
                    start = end
                widget.insert("end", "\n")
            widget.configure(state="disabled")
            if at_bottom:
                widget.see("end")
        except tk.TclError:
            pass       # widget went away mid-redraw (tab closed)

    # -- input -------------------------------------------------------------- #
    def _on_key(self, event) -> str:
        if self._on_input is None:
            return "break"
        data = self._encode(event)
        if data:
            try:
                self._on_input(data)
            except Exception:
                pass
        return "break"          # the shell echoes; never insert locally

    @staticmethod
    def _encode(event) -> str:
        """Translate a Tk key event into the bytes a shell expects."""
        keysym, ch, state = event.keysym, event.char, event.state
        ctrl = bool(state & 0x4)
        simple = {
            "Return": "\r", "KP_Enter": "\r", "Tab": "\t", "BackSpace": "\x7f",
            "Escape": "\x1b", "Delete": "\x1b[3~", "Up": "\x1b[A",
            "Down": "\x1b[B", "Right": "\x1b[C", "Left": "\x1b[D",
            "Home": "\x1b[H", "End": "\x1b[F", "Prior": "\x1b[5~",
            "Next": "\x1b[6~", "Insert": "\x1b[2~",
        }
        if keysym in simple:
            return simple[keysym]
        if keysym.startswith("F") and keysym[1:].isdigit():
            n = int(keysym[1:])
            if 1 <= n <= 4:
                return "\x1bO" + "PQRS"[n - 1]
            codes = {5: "15", 6: "17", 7: "18", 8: "19", 9: "20",
                     10: "21", 11: "23", 12: "24"}
            if n in codes:
                return f"\x1b[{codes[n]}~"
            return ""
        if ctrl and len(keysym) == 1 and keysym.isalpha():
            return chr(ord(keysym.upper()) - 64)      # Ctrl-A..Ctrl-Z
        if ch:
            return ch
        return ""

    # -- clipboard ---------------------------------------------------------- #
    def _copy_selection(self, _event=None) -> None:
        """Copy on select -- no Ctrl+C, which the shell needs for SIGINT."""
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return
        if not selected:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(selected)
        except tk.TclError:
            pass

    def _paste(self, _event=None) -> str:
        if self._on_input is None:
            return "break"
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return "break"
        if data:
            try:
                self._on_input(data)
            except Exception:
                pass
        return "break"

    # -- geometry ----------------------------------------------------------- #
    def fit(self) -> None:
        """Resize the buffer to the visible character grid."""
        try:
            font = self.text.cget("font")
            fnt = tk.font.Font(font=font) if hasattr(tk, "font") else None
            if fnt is None:
                return
            cw = max(1, fnt.measure("M"))
            ch = max(1, fnt.metrics("linespace"))
            cols = max(20, (self.text.winfo_width() - 12) // cw)
            rows = max(5, (self.text.winfo_height() - 8) // ch)
            self.screen.resize(rows, cols)
        except Exception:
            pass
