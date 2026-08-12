"""CLI parsing/behaviour and headless GUI guards (no server, no display)."""

import sys
import os

import pytest

from sshdeck import __main__ as cli
from sshdeck import gui
from sshdeck.errors import SSHDeckError


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHDECK_CONFIG_DIR", str(tmp_path / "cfg"))
    yield


def test_cli_session_add_list_remove(capsys):
    assert cli.main(["session", "add", "web", "example.com",
                     "--user", "deploy", "--auth", "key",
                     "--key", "/home/me/.ssh/id_ed25519"]) == 0
    assert cli.main(["session", "list"]) == 0
    out = capsys.readouterr().out
    assert "web" in out and "example.com" in out
    assert cli.main(["session", "remove", "web"]) == 0


def test_cli_keygen_and_pubkey(tmp_path, capsys):
    key = str(tmp_path / "id_ed25519")
    assert cli.main(["keygen", "--type", "ed25519", "--out", key]) == 0
    assert os.path.exists(key) and os.path.exists(key + ".pub")
    capsys.readouterr()
    assert cli.main(["pubkey", key]) == 0
    assert capsys.readouterr().out.startswith("ssh-ed25519 ")


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_cli_forward_describe(capsys):
    assert cli.main(["forward", "8080:localhost:80"]) == 0
    assert "8080" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_cli_run_unknown_session_exits_nonzero(capsys):
    rc = cli.main(["run", "nope", "uptime"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_gui_imports_without_display():
    # Importing the module must have no side effects; main() must be present.
    assert hasattr(gui, "main")


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_gui_main_headless_returns_zero(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert gui.main() == 0
