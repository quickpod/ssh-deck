r"""ANSI/xterm escape-sequence parser for the terminal view.

The shell does not send plain text.  It sends text interleaved with control
sequences: colour changes, cursor moves, window-title updates, bracketed-paste
toggles.  Writing that stream straight into a text widget prints the sequences
literally -- ``^[[?2004h``, ``^[]0;root@host: ~^G`` -- and loses every colour,
which is exactly what an unparsed terminal looks like.

:class:`AnsiParser` turns the stream into :class:`Span` objects carrying the
text plus the style in force, and reports the handful of control actions a
scrollback view can honour (erase, carriage return, bell, title).

Two properties matter as much as the parsing itself:

* **It is resumable.**  Data arrives in arbitrary chunks, so an escape
  sequence is routinely split across two reads.  The parser keeps its state
  between :meth:`feed` calls and holds an incomplete sequence back rather than
  emitting it as text -- the single most common way naive parsers leak
  ``[0m`` onto the screen.
* **Unknown sequences are swallowed, never printed.**  A terminal the user is
  actually working in should degrade by ignoring what it does not implement,
  not by spraying control codes into their output.

Pure and dependency-free, so it is testable without a GUI or a connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

BEL = "\x07"
ESC = "\x1b"

# --- actions the view is expected to honour -------------------------------- #
ERASE_DISPLAY = "erase_display"
ERASE_LINE = "erase_line"
CARRIAGE_RETURN = "carriage_return"
BACKSPACE = "backspace"
BELL = "bell"
SET_TITLE = "set_title"


#: The Ubuntu/GNOME palette -- what `ls` colours look like on the servers this
#: client actually talks to.  Index 0-7 normal, 8-15 bright.
UBUNTU_PALETTE: Tuple[str, ...] = (
    "#171421", "#c01c28", "#26a269", "#a2734c",
    "#12488b", "#a347ba", "#2aa1b3", "#d0cfcc",
    "#5e5c64", "#f66151", "#33d17a", "#e9ad0c",
    "#2a7bde", "#c061cb", "#33c7de", "#ffffff",
)
DEFAULT_FG = "#d0cfcc"
DEFAULT_BG = "#171421"


@dataclass(frozen=True)
class Style:
    """Text attributes in force for a span."""

    fg: Optional[str] = None          # None = terminal default
    bg: Optional[str] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    strike: bool = False

    def key(self) -> str:
        """A short stable name, used as the text-widget tag."""
        parts = [
            f"f{self.fg[1:]}" if self.fg else "f-",
            f"b{self.bg[1:]}" if self.bg else "b-",
        ]
        for flag, letter in ((self.bold, "B"), (self.dim, "D"),
                             (self.italic, "I"), (self.underline, "U"),
                             (self.reverse, "R"), (self.strike, "S")):
            if flag:
                parts.append(letter)
        return "ansi_" + "_".join(parts)

    def resolved(self) -> Tuple[str, str]:
        """Concrete (foreground, background), honouring reverse video."""
        fg = self.fg or DEFAULT_FG
        bg = self.bg or DEFAULT_BG
        if self.reverse:
            fg, bg = bg, fg
        if self.dim and not self.bold:
            fg = _blend(fg, bg, 0.45)
        return fg, bg


def _blend(a: str, b: str, t: float) -> str:
    """Mix two #rrggbb colours; used to render 'dim' without a second palette."""
    try:
        ar, ag, ab = (int(a[i:i + 2], 16) for i in (1, 3, 5))
        br, bg_, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return a
    mix = lambda x, y: int(round(x + (y - x) * t))  # noqa: E731
    return "#%02x%02x%02x" % (mix(ar, br), mix(ag, bg_), mix(ab, bb))


@dataclass
class Span:
    """A run of text sharing one style."""

    text: str
    style: Style = field(default_factory=Style)


@dataclass
class Action:
    """A control action the view should apply at this point in the stream."""

    kind: str
    value: Optional[str] = None


def _xterm256(index: int) -> str:
    """Resolve an xterm-256 colour index to #rrggbb."""
    if index < 16:
        return UBUNTU_PALETTE[index]
    if index < 232:                       # 6x6x6 colour cube
        index -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r, g, b = levels[index // 36], levels[(index // 6) % 6], levels[index % 6]
        return "#%02x%02x%02x" % (r, g, b)
    grey = 8 + (index - 232) * 10         # 24-step greyscale ramp
    return "#%02x%02x%02x" % (grey, grey, grey)


class AnsiParser:
    """Incremental ANSI/xterm parser.  One instance per terminal view."""

    def __init__(self) -> None:
        self.style = Style()
        self.title: str = ""
        self._pending = ""      # an incomplete sequence carried between feeds

    # -- public ---------------------------------------------------------- #
    def reset(self) -> None:
        self.style = Style()
        self.title = ""
        self._pending = ""

    def feed(self, data: str) -> List[object]:
        """Consume *data*; return an ordered list of Span and Action items.

        Anything that is not yet a complete escape sequence is retained for
        the next call rather than emitted, so sequences split across reads
        never reach the screen as text.
        """
        out: List[object] = []
        buf = self._pending + (data or "")
        self._pending = ""
        text: List[str] = []
        i, n = 0, len(buf)

        def flush() -> None:
            if text:
                out.append(Span("".join(text), self.style))
                text.clear()

        while i < n:
            ch = buf[i]
            if ch == ESC:
                consumed = self._escape(buf, i, out, flush)
                if consumed is None:        # incomplete -- wait for more data
                    self._pending = buf[i:]
                    break
                i = consumed
                continue
            if ch == "\r":
                flush(); out.append(Action(CARRIAGE_RETURN)); i += 1; continue
            if ch == "\b":
                flush(); out.append(Action(BACKSPACE)); i += 1; continue
            if ch == BEL:
                flush(); out.append(Action(BELL)); i += 1; continue
            if ch == "\t" or ch == "\n" or ch >= " " or ch == "\x00":
                if ch != "\x00":            # NUL is padding; drop it
                    text.append(ch)
                i += 1
                continue
            i += 1                          # other C0 control: ignore
        flush()
        return out

    # -- internals -------------------------------------------------------- #
    def _escape(self, buf: str, i: int, out: List[object], flush) -> Optional[int]:
        """Handle the escape at *i*; return the new index, or None if partial."""
        n = len(buf)
        if i + 1 >= n:
            return None
        kind = buf[i + 1]

        if kind == "[":                      # CSI
            j = i + 2
            while j < n and not ("@" <= buf[j] <= "~"):
                j += 1
            if j >= n:
                return None
            self._csi(buf[i + 2:j], buf[j], out, flush)
            return j + 1

        if kind == "]":                      # OSC -- ends at BEL or ESC \
            j = i + 2
            while j < n:
                if buf[j] == BEL:
                    self._osc(buf[i + 2:j], out, flush)
                    return j + 1
                if buf[j] == ESC and j + 1 < n and buf[j + 1] == "\\":
                    self._osc(buf[i + 2:j], out, flush)
                    return j + 2
                if buf[j] == ESC and j + 1 >= n:
                    return None
                j += 1
            return None

        if kind in "()#%":                   # charset selection: 2-byte payload
            return i + 3 if i + 2 < n else None
        if kind == "P" or kind == "^" or kind == "_":   # DCS/PM/APC to ST
            j = buf.find(ESC + "\\", i + 2)
            return j + 2 if j != -1 else None
        # Simple two-character escapes (RIS, IND, NEL, DECSC...): ignore.
        return i + 2

    def _csi(self, params: str, final: str, out: List[object], flush) -> None:
        if params.startswith("?"):
            # Private modes: bracketed paste (2004), cursor visibility (25),
            # alt screen (1049)... none change how text is rendered here.
            return
        if final == "m":
            # Emit what came before under the OLD style; anything after this
            # point belongs to the new one. Without this every span would take
            # the last style seen in the chunk and all colour would collapse.
            flush()
            self._sgr(params)
            return
        if final == "J":
            flush(); out.append(Action(ERASE_DISPLAY, params or "0")); return
        if final == "K":
            flush(); out.append(Action(ERASE_LINE, params or "0")); return
        # Cursor movement, scrolling regions, device reports: a scrollback
        # view has no cursor to move, so they are ignored rather than printed.

    def _osc(self, body: str, out: List[object], flush) -> None:
        code, _, text = body.partition(";")
        if code in ("0", "1", "2"):          # icon name / window title
            self.title = text
            flush(); out.append(Action(SET_TITLE, text))

    def _sgr(self, params: str) -> None:
        """Apply a Select Graphic Rendition sequence to the current style."""
        codes = [p for p in params.split(";")]
        if not params:
            codes = ["0"]
        i = 0
        style = self.style
        while i < len(codes):
            raw = codes[i].strip()
            i += 1
            if raw == "":
                raw = "0"
            try:
                code = int(raw)
            except ValueError:
                continue
            if code == 0:
                style = Style()
            elif code == 1:
                style = replace(style, bold=True)
            elif code == 2:
                style = replace(style, dim=True)
            elif code == 3:
                style = replace(style, italic=True)
            elif code == 4:
                style = replace(style, underline=True)
            elif code == 7:
                style = replace(style, reverse=True)
            elif code == 9:
                style = replace(style, strike=True)
            elif code == 22:
                style = replace(style, bold=False, dim=False)
            elif code == 23:
                style = replace(style, italic=False)
            elif code == 24:
                style = replace(style, underline=False)
            elif code == 27:
                style = replace(style, reverse=False)
            elif code == 29:
                style = replace(style, strike=False)
            elif 30 <= code <= 37:
                style = replace(style, fg=UBUNTU_PALETTE[code - 30])
            elif 90 <= code <= 97:
                style = replace(style, fg=UBUNTU_PALETTE[code - 90 + 8])
            elif 40 <= code <= 47:
                style = replace(style, bg=UBUNTU_PALETTE[code - 40])
            elif 100 <= code <= 107:
                style = replace(style, bg=UBUNTU_PALETTE[code - 100 + 8])
            elif code == 39:
                style = replace(style, fg=None)
            elif code == 49:
                style = replace(style, bg=None)
            elif code in (38, 48):
                colour, i = self._extended(codes, i)
                if colour is not None:
                    style = (replace(style, fg=colour) if code == 38
                             else replace(style, bg=colour))
        self.style = style

    @staticmethod
    def _extended(codes: List[str], i: int) -> Tuple[Optional[str], int]:
        """Parse 38/48's ``;5;n`` (256-colour) or ``;2;r;g;b`` (truecolour)."""
        def nxt() -> Optional[int]:
            nonlocal i
            if i >= len(codes):
                return None
            try:
                value = int(codes[i] or 0)
            except ValueError:
                value = None
            i += 1
            return value

        mode = nxt()
        if mode == 5:
            index = nxt()
            if index is None or not 0 <= index <= 255:
                return None, i
            return _xterm256(index), i
        if mode == 2:
            r, g, b = nxt(), nxt(), nxt()
            if None in (r, g, b):
                return None, i
            clamp = lambda v: max(0, min(255, v))  # noqa: E731
            return "#%02x%02x%02x" % (clamp(r), clamp(g), clamp(b)), i
        return None, i


def plain_text(data: str) -> str:
    """Strip every escape sequence from *data* (handy for logs and tests)."""
    parser = AnsiParser()
    return "".join(item.text for item in parser.feed(data)
                   if isinstance(item, Span))
