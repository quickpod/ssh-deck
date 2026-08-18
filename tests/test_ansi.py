r"""ANSI/xterm parsing for the terminal view.

Pure parsing: no GUI, no connection. The fixtures are real sequences captured
from an Ubuntu login shell, because that is what these servers actually send.
"""

from __future__ import annotations

import pytest

from sshdeck import ansi
from sshdeck.ansi import (Action, AnsiParser, Span, BELL, CARRIAGE_RETURN,
                          ERASE_DISPLAY, ERASE_LINE, SET_TITLE, plain_text)


def spans(items):
    return [i for i in items if isinstance(i, Span)]


def actions(items):
    return [i for i in items if isinstance(i, Action)]


def text_of(items):
    return "".join(s.text for s in spans(items))


# --------------------------------------------------------------------------- #
# The garbage from the field report
# --------------------------------------------------------------------------- #
def test_bracketed_paste_and_title_never_reach_the_screen():
    """The exact stream that was printing as [?2004h / 0;root@host junk."""
    raw = "\x1b[?2004h\x1b]0;root@gpu: ~\x07root@gpu:~# \x1b[?2004l"
    out = AnsiParser().feed(raw)
    assert text_of(out) == "root@gpu:~# "
    assert "2004" not in text_of(out)


def test_window_title_is_reported_not_printed():
    out = AnsiParser().feed("\x1b]0;user@host: ~/work\x07$ ")
    titles = [a.value for a in actions(out) if a.kind == SET_TITLE]
    assert titles == ["user@host: ~/work"]
    assert text_of(out) == "$ "


def test_osc_terminated_by_string_terminator_also_works():
    out = AnsiParser().feed("\x1b]0;title\x1b\\rest")
    assert text_of(out) == "rest"
    assert [a.value for a in actions(out) if a.kind == SET_TITLE] == ["title"]


def test_plain_text_helper_strips_everything():
    raw = "\x1b[01;34mDocs\x1b[0m \x1b[?2004h\x1b]0;t\x07x"
    assert plain_text(raw) == "Docs x"


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
def test_ls_colours_split_into_styled_spans():
    """`ls` output must colour per name, not collapse into one run."""
    out = AnsiParser().feed(
        "\x1b[01;34mDocuments\x1b[0m  \x1b[01;32mrun.sh\x1b[0m\n")
    coloured = [s for s in spans(out) if s.text.strip()]
    assert coloured[0].text == "Documents"
    assert coloured[0].style.bold is True
    assert coloured[0].style.fg == ansi.UBUNTU_PALETTE[4]     # blue = directory
    assert coloured[1].text == "run.sh"
    assert coloured[1].style.fg == ansi.UBUNTU_PALETTE[2]     # green = executable


def test_style_does_not_bleed_past_a_reset():
    out = AnsiParser().feed("\x1b[31mred\x1b[0mplain")
    got = {s.text: s.style.fg for s in spans(out)}
    assert got["red"] == ansi.UBUNTU_PALETTE[1]
    assert got["plain"] is None


def test_bright_foreground_and_background():
    out = AnsiParser().feed("\x1b[91;44mx")
    s = spans(out)[0].style
    assert s.fg == ansi.UBUNTU_PALETTE[9]
    assert s.bg == ansi.UBUNTU_PALETTE[4]


def test_256_colour():
    out = AnsiParser().feed("\x1b[38;5;208mx")
    assert spans(out)[0].style.fg == "#ff8700"


def test_truecolour():
    out = AnsiParser().feed("\x1b[38;2;18;52;86mx")
    assert spans(out)[0].style.fg == "#123456"


def test_reverse_video_swaps_resolved_colours():
    out = AnsiParser().feed("\x1b[31;47;7mx")
    fg, bg = spans(out)[0].style.resolved()
    assert fg == ansi.UBUNTU_PALETTE[7] and bg == ansi.UBUNTU_PALETTE[1]


def test_attributes_can_be_switched_off_individually():
    out = AnsiParser().feed("\x1b[1;4mboth\x1b[24monly-bold")
    got = {s.text: s.style for s in spans(out)}
    assert got["both"].bold and got["both"].underline
    assert got["only-bold"].bold and not got["only-bold"].underline


def test_style_key_is_stable_and_distinct():
    a = ansi.Style(fg="#ff0000", bold=True)
    assert a.key() == ansi.Style(fg="#ff0000", bold=True).key()
    assert a.key() != ansi.Style(fg="#ff0000").key()


# --------------------------------------------------------------------------- #
# Resumability -- sequences split across reads
# --------------------------------------------------------------------------- #
def test_sequence_split_across_chunks_is_not_leaked():
    p = AnsiParser()
    first = p.feed("hello \x1b[01;3")
    second = p.feed("4mworld")
    assert text_of(first) == "hello "
    assert text_of(second) == "world"
    assert spans(second)[0].style.fg == ansi.UBUNTU_PALETTE[4]


def test_escape_at_the_very_end_of_a_chunk():
    p = AnsiParser()
    assert text_of(p.feed("abc\x1b")) == "abc"
    assert text_of(p.feed("[0mdef")) == "def"


def test_osc_split_across_chunks():
    p = AnsiParser()
    p.feed("\x1b]0;my ti")
    out = p.feed("tle\x07done")
    assert text_of(out) == "done"
    assert p.title == "my title"


def test_style_persists_across_feeds():
    p = AnsiParser()
    p.feed("\x1b[32m")
    assert spans(p.feed("green"))[0].style.fg == ansi.UBUNTU_PALETTE[2]


# --------------------------------------------------------------------------- #
# Control actions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,kind", [
    ("\x1b[2J", ERASE_DISPLAY),
    ("\x1b[K", ERASE_LINE),
    ("\r", CARRIAGE_RETURN),
    ("\x07", BELL),
])
def test_control_actions_are_reported(raw, kind):
    assert any(a.kind == kind for a in actions(AnsiParser().feed(raw)))


def test_cursor_moves_are_ignored_not_printed():
    out = AnsiParser().feed("\x1b[2;5Hx\x1b[Ay")
    assert text_of(out) == "xy"


def test_unknown_sequences_are_swallowed():
    """Degrade by ignoring, never by spraying control codes at the user."""
    assert text_of(AnsiParser().feed("\x1b[>99;1cvisible")) == "visible"
    assert text_of(AnsiParser().feed("\x1b(Bvisible")) == "visible"


def test_newlines_survive_and_tab_becomes_an_action():
    """A screen buffer must place tabs on real tab stops, so TAB is an action
    rather than a literal character."""
    out = AnsiParser().feed("a\tb\nc")
    assert text_of(out) == "ab\nc"
    assert any(a.kind == ansi.TAB for a in actions(out))


def test_nul_padding_is_dropped():
    assert text_of(AnsiParser().feed("a\x00b")) == "ab"


def test_reset_clears_style_and_title():
    p = AnsiParser()
    p.feed("\x1b]0;t\x07\x1b[31m")
    p.reset()
    assert p.title == "" and p.style == ansi.Style()
    assert spans(p.feed("x"))[0].style.fg is None


def test_empty_and_malformed_input_are_harmless():
    p = AnsiParser()
    assert p.feed("") == []
    assert text_of(p.feed("\x1b[;;;mx")) == "x"
    assert text_of(p.feed("\x1b[999mx")) == "x"
