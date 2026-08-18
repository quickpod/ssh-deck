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
