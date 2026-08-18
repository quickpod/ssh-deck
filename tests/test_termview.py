r"""Terminal widget behaviour that can be checked without a display.

The key encoder is the part most likely to break silently -- a wrong byte for
Ctrl+C or an arrow key is invisible in review and maddening in use -- so it is
a pure static method and tested directly.
"""

from __future__ import annotations

import pytest

from sshdeck.termview import TerminalView


class Event:
    """Minimal stand-in for a Tk key event."""

    def __init__(self, keysym="", char="", state=0):
        self.keysym, self.char, self.state = keysym, char, state


def enc(keysym="", char="", state=0):
    return TerminalView._encode(Event(keysym, char, state))


# --------------------------------------------------------------------------- #
# Control keys the shell depends on
# --------------------------------------------------------------------------- #
def test_ctrl_c_sends_sigint_not_a_copy():
    """Ctrl+C must reach the remote program -- hence copy-on-select instead."""
    assert enc("c", "c", state=0x4) == "\x03"


@pytest.mark.parametrize("letter,expected", [
    ("d", "\x04"), ("z", "\x1a"), ("l", "\x0c"), ("a", "\x01"),
])
def test_other_control_combinations(letter, expected):
    assert enc(letter, letter, state=0x4) == expected


def test_plain_letters_are_sent_as_typed():
    assert enc("a", "a") == "a"
    assert enc("A", "A") == "A"


def test_enter_sends_carriage_return():
    """CR, not LF: the remote pty translates it."""
    assert enc("Return") == "\r"


def test_backspace_sends_del():
    assert enc("BackSpace") == "\x7f"


# --------------------------------------------------------------------------- #
# Cursor and editing keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("keysym,expected", [
    ("Up", "\x1b[A"), ("Down", "\x1b[B"),
    ("Right", "\x1b[C"), ("Left", "\x1b[D"),
    ("Home", "\x1b[H"), ("End", "\x1b[F"),
    ("Prior", "\x1b[5~"), ("Next", "\x1b[6~"),
    ("Delete", "\x1b[3~"), ("Insert", "\x1b[2~"),
    ("Escape", "\x1b"), ("Tab", "\t"),
])
def test_navigation_keys(keysym, expected):
    assert enc(keysym) == expected


@pytest.mark.parametrize("keysym,expected", [
    ("F1", "\x1bOP"), ("F4", "\x1bOS"), ("F5", "\x1b[15~"), ("F12", "\x1b[24~"),
])
def test_function_keys(keysym, expected):
    assert enc(keysym) == expected


def test_unmapped_function_key_sends_nothing():
    assert enc("F20") == ""


def test_modifier_presses_alone_send_nothing():
    for keysym in ("Shift_L", "Control_L", "Alt_L", "Super_L"):
        assert enc(keysym) == ""


# --------------------------------------------------------------------------- #
# Selection must not freeze the screen
# --------------------------------------------------------------------------- #
import os                                        # noqa: E402

import pytest as _pytest                         # noqa: E402

tk = _pytest.importorskip("tkinter")

from sshdeck.termview import TerminalView as _TV  # noqa: E402


def _settle(root, seconds=0.4):
    """Let scheduled repaints actually run -- update() does not fire timers."""
    import time as _t
    end = _t.time() + seconds
    while _t.time() < end:
        root.update()
        _t.sleep(0.02)


@_pytest.fixture
def view():
    if not os.environ.get("DISPLAY") and os.name != "nt":
        _pytest.skip("no display")
    try:
        root = tk.Tk()
    except tk.TclError:
        _pytest.skip("no usable display")
    root.withdraw()
    sent = []
    v = _TV(root, on_input=sent.append)
    v.pack()
    v.feed("root@host:~# ")
    v._redraw()
    root.update()
    yield root, v, sent
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_paste_repaints_even_with_a_selection_left_over(view):
    """Copy leaves a selection; pasting must not leave the screen frozen."""
    root, v, sent = view
    v.text.tag_add("sel", "1.0", "1.5")
    root.update()
    root.clipboard_clear()
    root.clipboard_append("ls -la")
    root.update()
    v._paste()
    v.feed("ls -la")
    _settle(root)
    assert sent == ["ls -la"]
    assert not v.text.tag_ranges("sel"), "selection survived the paste"
    assert "ls -la" in v.text.get("1.0", "end")


def test_typing_clears_the_selection_and_unblocks_output(view):
    root, v, sent = view
    v.text.tag_add("sel", "1.0", "1.4")
    root.update()

    class Event:
        keysym, char, state = "a", "a", 0

    v._on_key(Event())
    root.update()
    assert not v.text.tag_ranges("sel")
    assert sent[-1] == "a"


def test_a_forgotten_selection_cannot_freeze_the_terminal(view):
    """Output eventually wins; a dead-looking terminal is worse than a lost
    highlight."""
    import time as _time
    root, v, _sent = view
    v.text.tag_add("sel", "1.0", "1.4")
    root.update()
    v.feed("held back")
    v._redraw()
    assert v._redraw_pending is True
    v._defer_started = _time.monotonic() - 99      # long past the cap
    v._redraw()
    _settle(root)
    assert "held back" in v.text.get("1.0", "end")
