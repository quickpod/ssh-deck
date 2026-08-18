r"""Session tree + tab strip.

Needs a display for the Tk widgets, so the whole module skips without one
rather than failing a headless CI run.
"""

from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter")

from sshdeck import navigator as nav          # noqa: E402
from sshdeck.navigator import (ACTIVITY, CONNECTED, CONNECTING, DISCONNECTED,
                               FAILED, SessionTree, TabStrip)  # noqa: E402
from sshdeck.sessions import Session          # noqa: E402


@pytest.fixture
def root():
    if not os.environ.get("DISPLAY") and os.name != "nt":
        pytest.skip("no display")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no usable display")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def test_every_state_has_a_distinct_glyph_and_colour():
    glyphs = {s["glyph"] for s in nav.STATE_STYLE.values()}
    colours = {s["colour"] for s in nav.STATE_STYLE.values()}
    assert len(glyphs) == len(nav.STATE_STYLE)
    assert len(colours) == len(nav.STATE_STYLE)


def test_unknown_state_falls_back_rather_than_raising():
    assert nav.state_style("nonsense") == nav.STATE_STYLE[DISCONNECTED]


def test_long_tab_labels_keep_their_tail():
    """The end of the path identifies a tab better than the host does."""
    out = TabStrip._elide("root@host: ~/a/very/long/path/that/keeps/going/on")
    assert out.startswith("…") and out.endswith("going/on")


def test_short_labels_are_untouched():
    assert TabStrip._elide("root@host: ~") == "root@host: ~"


# --------------------------------------------------------------------------- #
# Tab strip
# --------------------------------------------------------------------------- #
def test_first_tab_becomes_active(root):
    strip = TabStrip(root)
    strip.add("t1", "one")
    assert strip.active == "t1"


def test_adding_the_same_id_twice_is_ignored(root):
    strip = TabStrip(root)
    strip.add("t1", "one"); strip.add("t1", "again")
    assert strip.ids() == ["t1"]
    assert strip.label_of("t1") == "one"


def test_state_and_label_update(root):
    strip = TabStrip(root)
    strip.add("t1", "one", state=CONNECTING)
    strip.set_state("t1", CONNECTED)
    strip.set_label("t1", "root@host: ~/work")
    assert strip.state_of("t1") == CONNECTED
    assert strip.label_of("t1") == "root@host: ~/work"


def test_background_output_marks_activity(root):
    strip = TabStrip(root)
    strip.add("a", "A"); strip.add("b", "B")
    strip.select("a")
    strip.set_state("b", CONNECTED)
    strip.mark_activity("b")
    assert strip.state_of("b") == ACTIVITY


def test_the_active_tab_is_never_marked(root):
    """It is already being watched; a badge there is noise."""
    strip = TabStrip(root)
    strip.add("a", "A"); strip.set_state("a", CONNECTED)
    strip.select("a")
    strip.mark_activity("a")
    assert strip.state_of("a") == CONNECTED


def test_selecting_a_tab_clears_its_activity_mark(root):
    strip = TabStrip(root)
    strip.add("a", "A"); strip.add("b", "B")
    strip.select("a")
    strip.set_state("b", CONNECTED); strip.mark_activity("b")
    strip.select("b")
    assert strip.state_of("b") == CONNECTED


def test_activity_does_not_overwrite_a_failure(root):
    """A failed tab must keep saying failed."""
    strip = TabStrip(root)
    strip.add("a", "A"); strip.add("b", "B")
    strip.select("a")
    strip.set_state("b", FAILED)
    strip.mark_activity("b")
    assert strip.state_of("b") == FAILED


def test_closing_the_active_tab_activates_another(root):
    strip = TabStrip(root)
    strip.add("a", "A"); strip.add("b", "B")
    strip.select("b")
    strip.remove("b")
    assert strip.active == "a"


def test_closing_the_last_tab_leaves_none_active(root):
    strip = TabStrip(root)
    strip.add("a", "A")
    strip.remove("a")
    assert strip.active is None and strip.ids() == []


def test_close_callback_is_used_when_supplied(root):
    seen = []
    strip = TabStrip(root, on_close=seen.append)
    strip.add("a", "A")
    strip._close("a")
    assert seen == ["a"]
    assert strip.ids() == ["a"]      # the app decides whether to remove it


# --------------------------------------------------------------------------- #
# Session tree
# --------------------------------------------------------------------------- #
def _sessions():
    return [Session(name="alpha", host="a.example.com"),
            Session(name="beta", host="b.example.com"),
            Session(name="gamma", host="c.example.net")]


def test_tree_lists_every_session(root):
    tree = SessionTree(root)
    tree.set_sessions(_sessions())
    names = tree.tree.get_children("__root__")
    assert set(names) == {"alpha", "beta", "gamma"}


def test_filter_matches_name_or_host(root):
    tree = SessionTree(root)
    tree.set_sessions(_sessions())
    tree.filter_var.set("beta")
    assert set(tree.tree.get_children("__root__")) == {"beta"}
    tree.filter_var.set("example.net")
    assert set(tree.tree.get_children("__root__")) == {"gamma"}


def test_filter_is_case_insensitive(root):
    tree = SessionTree(root)
    tree.set_sessions(_sessions())
    tree.filter_var.set("ALPHA")
    assert set(tree.tree.get_children("__root__")) == {"alpha"}


def test_double_click_connects_the_selection(root):
    connected = []
    tree = SessionTree(root, on_connect=connected.append)
    tree.set_sessions(_sessions())
    tree.tree.selection_set("beta")
    tree._double_click()
    assert connected == ["beta"]


def test_double_click_with_nothing_selected_does_nothing(root):
    connected = []
    tree = SessionTree(root, on_connect=connected.append)
    tree.set_sessions(_sessions())
    tree._double_click()
    assert connected == []


def test_the_root_folder_is_not_connectable(root):
    """Clicking the 'Sessions' node must not try to open a connection."""
    connected = []
    tree = SessionTree(root, on_connect=connected.append)
    tree.set_sessions(_sessions())
    tree.tree.selection_set("__root__")
    tree._double_click()
    assert connected == []
