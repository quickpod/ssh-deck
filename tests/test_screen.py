r"""Cursor-addressed screen buffer.

These are the behaviours full-screen programs depend on; getting any of them
wrong shows up as smeared status lines or a shell that does not come back
after quitting vi.
"""

from __future__ import annotations

import pytest

from sshdeck.screen import Screen, BLANK


def scr(rows=6, cols=20, **kw):
    return Screen(rows=rows, cols=cols, **kw)


# --------------------------------------------------------------------------- #
# Basics
# --------------------------------------------------------------------------- #
def test_plain_text_lands_on_the_grid():
    s = scr(); s.feed("hello")
    assert s.text_rows()[0] == "hello"
    assert (s.row, s.col) == (0, 5)


def test_newline_and_carriage_return():
    s = scr(); s.feed("one\r\ntwo")
    assert s.text_rows()[:2] == ["one", "two"]


def test_backspace_moves_without_erasing():
    s = scr(); s.feed("abc\b")
    assert (s.row, s.col) == (0, 2)
    assert s.text_rows()[0] == "abc"


def test_tab_goes_to_the_next_tab_stop():
    s = scr(cols=40); s.feed("ab\tc")
    assert s.text_rows()[0] == "ab      c"     # column 8


def test_cursor_addressing_overwrites():
    s = scr(); s.feed("line one\x1b[1;1HTOP")
    assert s.text_rows()[0] == "TOPe one"


def test_cursor_is_clamped_to_the_grid():
    s = scr(rows=4, cols=10); s.feed("\x1b[99;99H")
    assert (s.row, s.col) == (3, 9)


# --------------------------------------------------------------------------- #
# Wrapping -- deferred, like xterm
# --------------------------------------------------------------------------- #
def test_writing_the_last_column_does_not_wrap_yet():
    """Eager wrapping inserts phantom blank lines in real programs."""
    s = scr(rows=3, cols=5); s.feed("abcde")
    assert s.text_rows()[0] == "abcde"
    assert s.row == 0


def test_the_next_character_after_the_edge_wraps():
    s = scr(rows=3, cols=5); s.feed("abcdef")
    assert s.text_rows()[0] == "abcde"
    assert s.text_rows()[1] == "f"
    assert s.row == 1


def test_carriage_return_cancels_a_pending_wrap():
    s = scr(rows=3, cols=5); s.feed("abcde\rX")
    assert s.text_rows()[0] == "Xbcde"
    assert s.row == 0


# --------------------------------------------------------------------------- #
# Erase
# --------------------------------------------------------------------------- #
def test_erase_to_end_of_line():
    s = scr(); s.feed("abcdef\x1b[1;4H\x1b[K")
    assert s.text_rows()[0] == "abc"


def test_erase_to_start_of_line():
    s = scr(); s.feed("abcdef\x1b[1;4H\x1b[1K")
    assert s.text_rows()[0].endswith("ef")
    assert s.text_rows()[0][:4] == "    "


def test_erase_whole_display():
    s = scr(); s.feed("a\r\nb\r\nc\x1b[2J")
    assert s.text_rows() == [""] * s.rows


def test_erase_from_cursor_down():
    s = scr(); s.feed("one\r\ntwo\r\nthree\x1b[2;2H\x1b[J")
    assert s.text_rows()[0] == "one"
    assert s.text_rows()[1] == "t"
    assert s.text_rows()[2] == ""


def test_erase_characters_in_place():
    s = scr(); s.feed("abcdef\x1b[1;2H\x1b[3X")
    assert s.text_rows()[0] == "a   ef"


# --------------------------------------------------------------------------- #
# Insert / delete
# --------------------------------------------------------------------------- #
def test_insert_and_delete_characters():
    s = scr(); s.feed("abcd\x1b[1;2H\x1b[2@")
    assert s.text_rows()[0] == "a  bcd"
    s2 = scr(); s2.feed("abcd\x1b[1;2H\x1b[2P")
    assert s2.text_rows()[0] == "ad"


def test_insert_and_delete_lines():
    s = scr(rows=4); s.feed("one\r\ntwo\r\nthree\x1b[2;1H\x1b[L")
    assert s.text_rows()[:3] == ["one", "", "two"]
    s2 = scr(rows=4); s2.feed("one\r\ntwo\r\nthree\x1b[2;1H\x1b[M")
    assert s2.text_rows()[:2] == ["one", "three"]


# --------------------------------------------------------------------------- #
# Scrolling regions -- what keeps top's header from smearing
# --------------------------------------------------------------------------- #
def test_scrolling_region_confines_the_scroll():
    s = scr(rows=5, cols=10)
    s.feed("h1\r\nh2\r\nb1\r\nb2\r\nb3")
    s.feed("\x1b[3;5r")            # region = rows 3..5
    s.feed("\x1b[5;1H\n")          # line feed at the bottom of the region
    rows = s.text_rows()
    assert rows[0] == "h1" and rows[1] == "h2"   # header untouched
    assert rows[2] == "b2" and rows[3] == "b3"


def test_setting_a_region_homes_the_cursor():
    s = scr(rows=6); s.feed("\x1b[2;5r")
    assert (s.row, s.col) == (1, 0)


def test_invalid_region_resets_to_full_screen():
    s = scr(rows=6); s.feed("\x1b[5;2r")
    assert (s.scroll_top, s.scroll_bottom) == (0, 5)


def test_reverse_index_scrolls_down_at_the_top():
    s = scr(rows=3, cols=6); s.feed("a\r\nb\r\nc\x1b[1;1H\x1bM")
    assert s.text_rows()[:2] == ["", "a"]


# --------------------------------------------------------------------------- #
# Scrollback
# --------------------------------------------------------------------------- #
def test_lines_scrolled_off_reach_scrollback():
    s = scr(rows=3, cols=10)
    s.feed("l1\r\nl2\r\nl3\r\nl4")
    assert "l1" in s.all_text()
    assert "l1" not in s.text()


def test_region_scrolls_do_not_pollute_scrollback():
    """Only whole-screen scrolls are history; a status-line scroll is not."""
    s = scr(rows=5, cols=10)
    s.feed("\x1b[2;4r\x1b[4;1H\n\n\n")
    assert s.all_text().count("\n") < 20
    assert len(s.scrollback) == 0


# --------------------------------------------------------------------------- #
# Alternate screen -- vi/top must restore the shell exactly
# --------------------------------------------------------------------------- #
def test_alt_screen_starts_blank_and_restores_on_exit():
    s = scr(rows=4, cols=12)
    s.feed("shell prompt$ ")
    before = s.text_rows()
    cursor_before = (s.row, s.col)
    s.feed("\x1b[?1049h")
    assert s.in_alt_screen is True
    assert s.text_rows() == [""] * s.rows
    s.feed("VI CONTENT")
    s.feed("\x1b[?1049l")
    assert s.in_alt_screen is False
    assert s.text_rows() == before
    assert (s.row, s.col) == cursor_before


def test_alt_screen_never_writes_scrollback():
    s = scr(rows=3, cols=8)
    s.feed("\x1b[?1049h")
    for i in range(10):
        s.feed(f"row{i}\r\n")
    assert len(s.scrollback) == 0


# --------------------------------------------------------------------------- #
# Cursor save/restore, visibility, title, bell
# --------------------------------------------------------------------------- #
def test_cursor_save_and_restore():
    s = scr(); s.feed("\x1b[3;4H\x1b7\x1b[1;1H\x1b8")
    assert (s.row, s.col) == (2, 3)


def test_cursor_visibility_is_tracked():
    s = scr(); s.feed("\x1b[?25l")
    assert s.cursor_visible is False
    s.feed("\x1b[?25h")
    assert s.cursor_visible is True


def test_title_and_bell_are_captured():
    # The first BEL terminates the OSC; only the second is an actual bell.
    s = scr(); s.feed("\x1b]0;root@host: ~\x07\x07")
    assert s.title == "root@host: ~"
    assert s.bell_count == 1


# --------------------------------------------------------------------------- #
# Style + resize
# --------------------------------------------------------------------------- #
def test_cells_carry_their_style():
    s = scr(); s.feed("\x1b[31mred")
    ch, style = s.grid[0][0]
    assert ch == "r" and style.fg is not None


def test_resize_preserves_content_and_clamps_cursor():
    s = scr(rows=5, cols=20); s.feed("hello world")
    s.resize(3, 8)
    assert s.text_rows()[0] == "hello wo"
    assert s.row < 3 and s.col < 8


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #
def test_split_sequences_still_work_on_the_screen():
    s = scr(); s.feed("\x1b[1;"); s.feed("1HX")
    assert s.text_rows()[0].startswith("X")


def test_garbage_does_not_raise():
    s = scr()
    s.feed("\x1b[999;999H\x1b[-1J\x1b[abc m\x1b[0;;;m ok")
    assert "ok" in s.text()
