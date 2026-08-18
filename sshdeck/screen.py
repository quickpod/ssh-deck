r"""A cursor-addressed screen buffer -- what makes vi, top and htop render.

A scrollback view only ever appends, which is fine for a shell prompt and
useless for anything full-screen: those programs paint a *grid*, moving the
cursor about, erasing regions and scrolling a sub-rectangle of the display.
:class:`Screen` is that grid.  It consumes the actions
:mod:`sshdeck.ansi` produces and keeps the visible rows in the state the
remote program intends.

Design notes worth knowing before changing anything here:

* **Two buffers.**  Full-screen programs switch to the *alternate* screen so
  that quitting them restores the shell exactly as it was.  The main buffer
  keeps scrollback; the alternate one deliberately does not, which is why
  ``top`` does not fill your history with redraws.
* **The scrolling region is honoured.**  ``top`` and ``vi`` set a region and
  scroll only inside it; ignoring that is what makes status lines smear.
* **Wrapping is deferred.**  A character written in the last column leaves the
  cursor *pending* at the edge rather than wrapping immediately, matching
  xterm.  Wrapping eagerly inserts phantom blank lines in real programs.

Everything is pure Python with no GUI dependency, so it is testable headless.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, List, Optional, Tuple

from . import ansi
from .ansi import Action, AnsiParser, Span, Style

DEFAULT_ROWS = 24
DEFAULT_COLS = 80
DEFAULT_SCROLLBACK = 5000
TAB_WIDTH = 8

#: One character cell: the glyph plus the style it was written with.
Cell = Tuple[str, Style]
BLANK: Cell = (" ", Style())


def _blank_row(cols: int) -> List[Cell]:
    return [BLANK] * cols


class Screen:
    """A VT-style screen buffer driven by :meth:`feed`."""

    def __init__(self, rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS,
                 scrollback: int = DEFAULT_SCROLLBACK) -> None:
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.parser = AnsiParser()
        self.title = ""
        self.cursor_visible = True
        self.bell_count = 0
        self._scrollback_limit = max(0, int(scrollback))
        self.scrollback: Deque[List[Cell]] = deque(maxlen=self._scrollback_limit or 1)
        self.grid: List[List[Cell]] = [_blank_row(self.cols) for _ in range(self.rows)]
        self.row = 0
        self.col = 0
        self._pending_wrap = False
        self._saved: Optional[Tuple[int, int, Style]] = None
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self._alt: Optional[dict] = None      # saved main buffer while in alt

    # -- properties -------------------------------------------------------- #
    @property
    def in_alt_screen(self) -> bool:
        return self._alt is not None

    @property
    def style(self) -> Style:
        return self.parser.style

    # -- feeding ----------------------------------------------------------- #
    def feed(self, data: str) -> None:
        """Apply a chunk of terminal output to the buffer."""
        for item in self.parser.feed(data):
            if isinstance(item, Span):
                self._write(item.text, item.style)
            elif isinstance(item, Action):
                self._action(item)

    # -- rendering --------------------------------------------------------- #
    def display_rows(self) -> List[List[Cell]]:
        """The visible grid (a copy, safe to hand to a renderer)."""
        return [list(r) for r in self.grid]

    def all_rows(self) -> List[List[Cell]]:
        """Scrollback followed by the visible grid."""
        return [list(r) for r in self.scrollback] + self.display_rows()

    def text_rows(self) -> List[str]:
        """The visible grid as plain strings, trailing blanks trimmed."""
        return ["".join(ch for ch, _ in row).rstrip() for row in self.grid]

    def text(self) -> str:
        return "\n".join(self.text_rows())

    def all_text(self) -> str:
        return "\n".join("".join(ch for ch, _ in row).rstrip()
                         for row in self.all_rows())

    # -- geometry ---------------------------------------------------------- #
    def resize(self, rows: int, cols: int) -> None:
        """Resize the grid, preserving content from the top-left."""
        rows, cols = max(1, int(rows)), max(1, int(cols))
        if rows == self.rows and cols == self.cols:
            return
        grid = []
        for r in range(rows):
            old = self.grid[r] if r < len(self.grid) else []
            row = list(old[:cols])
            row += [BLANK] * (cols - len(row))
            grid.append(row)
        self.grid = grid
        self.rows, self.cols = rows, cols
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols - 1)
        self._pending_wrap = False

    # -- writing ----------------------------------------------------------- #
    def _write(self, text: str, style: Style) -> None:
        for ch in text:
            if ch == "\n":
                self._line_feed()
                self.col = 0
                self._pending_wrap = False
                continue
            if self._pending_wrap:
                self.col = 0
                self._line_feed()
                self._pending_wrap = False
            if self.col >= self.cols:        # defensive; normally pending_wrap
                self.col = self.cols - 1
            self.grid[self.row][self.col] = (ch, style)
            if self.col + 1 >= self.cols:
                self._pending_wrap = True    # xterm: wrap on the *next* glyph
            else:
                self.col += 1

    def _line_feed(self) -> None:
        if self.row == self.scroll_bottom:
            self._scroll_up(1)
        elif self.row < self.rows - 1:
            self.row += 1

    def _reverse_index(self) -> None:
        if self.row == self.scroll_top:
            self._scroll_down(1)
        elif self.row > 0:
            self.row -= 1

    def _scroll_up(self, n: int) -> None:
        """Scroll the region up, retiring lines to scrollback where apt."""
        n = max(1, n)
        for _ in range(n):
            line = self.grid.pop(self.scroll_top)
            # Only the main buffer keeps history, and only when the region is
            # the whole screen -- otherwise a status-line scroll would pollute
            # scrollback with fragments.
            if (not self.in_alt_screen and self._scrollback_limit
                    and self.scroll_top == 0 and self.scroll_bottom == self.rows - 1):
                self.scrollback.append(line)
            self.grid.insert(self.scroll_bottom, _blank_row(self.cols))

    def _scroll_down(self, n: int) -> None:
        for _ in range(max(1, n)):
            self.grid.pop(self.scroll_bottom)
            self.grid.insert(self.scroll_top, _blank_row(self.cols))

    # -- actions ----------------------------------------------------------- #
    def _action(self, action: Action) -> None:
        kind, raw = action.kind, action.value
        A = ansi

        if kind == A.SET_TITLE:
            self.title = raw or ""
        elif kind == A.BELL:
            self.bell_count += 1
        elif kind == A.CARRIAGE_RETURN:
            self.col = 0
            self._pending_wrap = False
        elif kind == A.LINE_FEED:
            self._line_feed()
        elif kind == A.REVERSE_INDEX:
            self._reverse_index()
        elif kind == A.BACKSPACE:
            self._pending_wrap = False
            self.col = max(0, self.col - 1)
        elif kind == A.TAB:
            self._pending_wrap = False
            nxt = ((self.col // TAB_WIDTH) + 1) * TAB_WIDTH
            self.col = min(nxt, self.cols - 1)
        elif kind == A.CURSOR_VISIBLE:
            self.cursor_visible = raw == "1"
        elif kind == A.ALT_SCREEN:
            self._set_alt_screen(raw == "1")
        elif kind == A.CURSOR_POS:
            row, _, col = (raw or "").partition(";")
            self._goto(self._num(row, 1) - 1, self._num(col, 1) - 1)
        elif kind == A.CURSOR_UP:
            self._goto(self.row - self._num(raw, 1), self.col)
        elif kind == A.CURSOR_DOWN:
            self._goto(self.row + self._num(raw, 1), self.col)
        elif kind == A.CURSOR_FORWARD:
            self._goto(self.row, self.col + self._num(raw, 1))
        elif kind == A.CURSOR_BACK:
            self._goto(self.row, self.col - self._num(raw, 1))
        elif kind == A.CURSOR_COLUMN:
            self._goto(self.row, self._num(raw, 1) - 1)
        elif kind == A.CURSOR_SAVE:
            self._saved = (self.row, self.col, self.parser.style)
        elif kind == A.CURSOR_RESTORE:
            if self._saved:
                self.row, self.col, self.parser.style = self._saved
                self._clamp()
        elif kind == A.ERASE_DISPLAY:
            self._erase_display(raw or "0")
        elif kind == A.ERASE_LINE:
            self._erase_line(raw or "0")
        elif kind == A.ERASE_CHARS:
            n = self._num(raw, 1)
            for i in range(self.col, min(self.col + n, self.cols)):
                self.grid[self.row][i] = BLANK
        elif kind == A.INSERT_LINES:
            self._insert_lines(self._num(raw, 1))
        elif kind == A.DELETE_LINES:
            self._delete_lines(self._num(raw, 1))
        elif kind == A.INSERT_CHARS:
            n = self._num(raw, 1)
            row = self.grid[self.row]
            self.grid[self.row] = (row[:self.col] + [BLANK] * n
                                   + row[self.col:])[:self.cols]
        elif kind == A.DELETE_CHARS:
            n = self._num(raw, 1)
            row = self.grid[self.row]
            kept = row[:self.col] + row[self.col + n:]
            self.grid[self.row] = kept + [BLANK] * (self.cols - len(kept))
        elif kind == A.SCROLL_UP:
            self._scroll_up(self._num(raw, 1))
        elif kind == A.SCROLL_DOWN:
            self._scroll_down(self._num(raw, 1))
        elif kind == A.SET_SCROLL_REGION:
            self._set_region(raw)

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _num(raw: Optional[str], default: int) -> int:
        try:
            value = int((raw or "").strip() or default)
        except ValueError:
            return default
        return value if value > 0 else default

    def _clamp(self) -> None:
        self.row = max(0, min(self.row, self.rows - 1))
        self.col = max(0, min(self.col, self.cols - 1))

    def _goto(self, row: int, col: int) -> None:
        self.row, self.col = row, col
        self._pending_wrap = False
        self._clamp()

    def _set_region(self, raw: Optional[str]) -> None:
        top, _, bottom = (raw or "").partition(";")
        t = self._num(top, 1) - 1
        b = self._num(bottom, self.rows) - 1
        if 0 <= t < b < self.rows:
            self.scroll_top, self.scroll_bottom = t, b
        else:                                   # invalid: reset to full screen
            self.scroll_top, self.scroll_bottom = 0, self.rows - 1
        self._goto(self.scroll_top, 0)          # DECSTBM homes the cursor

    def _insert_lines(self, n: int) -> None:
        if not self.scroll_top <= self.row <= self.scroll_bottom:
            return
        for _ in range(n):
            self.grid.pop(self.scroll_bottom)
            self.grid.insert(self.row, _blank_row(self.cols))

    def _delete_lines(self, n: int) -> None:
        if not self.scroll_top <= self.row <= self.scroll_bottom:
            return
        for _ in range(n):
            self.grid.pop(self.row)
            self.grid.insert(self.scroll_bottom, _blank_row(self.cols))

    def _erase_display(self, mode: str) -> None:
        if mode == "0":                          # cursor to end
            self._erase_line("0")
            for r in range(self.row + 1, self.rows):
                self.grid[r] = _blank_row(self.cols)
        elif mode == "1":                        # start to cursor
            for r in range(0, self.row):
                self.grid[r] = _blank_row(self.cols)
            self._erase_line("1")
        else:                                    # 2/3: whole display
            self.grid = [_blank_row(self.cols) for _ in range(self.rows)]

    def _erase_line(self, mode: str) -> None:
        row = self.grid[self.row]
        if mode == "0":
            self.grid[self.row] = (row[:self.col]
                                   + [BLANK] * (self.cols - self.col))
        elif mode == "1":
            self.grid[self.row] = ([BLANK] * min(self.col + 1, self.cols)
                                   + row[self.col + 1:])
        else:
            self.grid[self.row] = _blank_row(self.cols)

    def _set_alt_screen(self, enable: bool) -> None:
        if enable and self._alt is None:
            self._alt = {"grid": self.grid, "row": self.row, "col": self.col,
                         "top": self.scroll_top, "bottom": self.scroll_bottom}
            self.grid = [_blank_row(self.cols) for _ in range(self.rows)]
            self._goto(0, 0)
            self.scroll_top, self.scroll_bottom = 0, self.rows - 1
        elif not enable and self._alt is not None:
            saved = self._alt
            self._alt = None
            self.grid = saved["grid"]
            # The shell must come back exactly as it was left.
            self.row, self.col = saved["row"], saved["col"]
            self.scroll_top, self.scroll_bottom = saved["top"], saved["bottom"]
            self._clamp()
