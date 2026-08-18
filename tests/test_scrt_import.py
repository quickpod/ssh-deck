r"""SecureCRT session import.

Fixtures write a miniature SecureCRT tree, so nothing here depends on a real
SecureCRT install (or on the developer having one).
"""

from __future__ import annotations

import os

import pytest

from sshdeck import scrt
from sshdeck.scrt import ImportError_

SSH_INI = '''﻿S:"Hostname"=host.example.com
S:"Username"=root
D:"Port"=00000016
S:"Protocol Name"=SSH2
S:"SSH2 Authentications V2"=publickey,keyboard-interactive,password
D:"Session Password Saved"=00000000
S:"Firewall Name"=None
S:"Emulation"=Xterm
B:"Linux Normal Font v2"=000000a0
 f2 ff ff ff 08 00 00 00
'''

PW_INI = '''S:"Hostname"=box.example.com
S:"Username"=quick
S:"Protocol Name"=SSH2
D:"Session Password Saved"=00000001
S:"Password V2"=03:deadbeefdeadbeef
S:"Firewall Name"=None
'''

LOCAL_INI = '''S:"Protocol Name"=Local Shell
S:"Username"=
'''


@pytest.fixture
def scrt_tree(tmp_path):
    cfg = tmp_path / "Config"
    sess = cfg / "Sessions"
    sess.mkdir(parents=True)
    (sess / "example.ini").write_text(SSH_INI, encoding="utf-8")
    (sess / "withpw.ini").write_text(PW_INI, encoding="utf-8")
    (sess / "Default_LocalShell.ini").write_text(LOCAL_INI, encoding="utf-8")
    (sess / "__FolderData__.ini").write_text(
        'S:"Session List"=withpw:example:\n', encoding="utf-8")
    return str(cfg)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_ini_reads_typed_keys():
    got = scrt.parse_ini(SSH_INI)
    assert got["Hostname"] == ("S", "host.example.com")
    assert got["Port"] == ("D", "00000016")


def test_parse_ini_skips_binary_continuation_lines():
    got = scrt.parse_ini(SSH_INI)
    assert "f2 ff ff ff 08 00 00 00" not in got
    assert got["Linux Normal Font v2"][0] == "B"


def test_parse_ini_tolerates_a_bom():
    assert "Hostname" in scrt.parse_ini(SSH_INI)   # SSH_INI starts with ﻿


# --------------------------------------------------------------------------- #
# Field mapping
# --------------------------------------------------------------------------- #
def test_port_is_read_as_hex(scrt_tree):
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "example.ini"))
    assert item.session.port == 0x16 == 22


def test_missing_port_falls_back_to_22(scrt_tree):
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "withpw.ini"))
    assert item.session.port == 22


def test_publickey_maps_to_key_auth(scrt_tree):
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "example.ini"))
    assert item.session.auth == "key"
    assert item.had_password is False


def test_saved_password_maps_to_password_auth_and_is_flagged(scrt_tree):
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "withpw.ini"))
    assert item.session.auth == "password"
    assert item.had_password is True
    assert item.needs_password is True


def test_the_encrypted_password_is_never_carried_across(scrt_tree):
    """The whole safety property: no SecureCRT ciphertext reaches our store."""
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "withpw.ini"))
    blob = repr(item.session.to_dict())
    assert "deadbeef" not in blob
    assert "03:" not in blob


def test_firewall_none_is_not_treated_as_a_jump_host(scrt_tree):
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "example.ini"))
    assert item.session.jump is None


def test_hostless_local_shell_sessions_are_skipped(scrt_tree):
    item = scrt.read_session_file(
        os.path.join(scrt_tree, "Sessions", "Default_LocalShell.ini"))
    assert item is None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_discover_finds_real_sessions_and_skips_builtins(scrt_tree):
    names = [i.session.name for i in scrt.discover(scrt_tree)]
    assert names == ["withpw", "example"]      # __FolderData__ order, defaults out


def test_discover_uses_subdirectories_as_folders(scrt_tree, tmp_path):
    sub = tmp_path / "Config" / "Sessions" / "Datacentre"
    sub.mkdir()
    (sub / "edge.ini").write_text(SSH_INI, encoding="utf-8")
    folders = {i.session.name: i.folder for i in scrt.discover(scrt_tree)}
    assert folders["edge"] == "Datacentre"


def test_one_bad_file_does_not_sink_the_whole_import(scrt_tree, tmp_path):
    bad = tmp_path / "Config" / "Sessions" / "broken.ini"
    bad.write_text('S:"Hostname"=\nS:"Username"=x\n', encoding="utf-8")
    assert [i.session.name for i in scrt.discover(scrt_tree)] == ["withpw", "example"]


def test_missing_config_dir_explains_where_to_look(tmp_path):
    with pytest.raises(ImportError_) as exc:
        scrt.discover(str(tmp_path / "nope"))
    assert "Sessions" in str(exc.value)


def test_import_never_writes_to_securecrts_files(scrt_tree):
    before = {p: os.stat(os.path.join(scrt_tree, "Sessions", p)).st_mtime_ns
              for p in os.listdir(os.path.join(scrt_tree, "Sessions"))}
    scrt.discover(scrt_tree)
    after = {p: os.stat(os.path.join(scrt_tree, "Sessions", p)).st_mtime_ns
             for p in os.listdir(os.path.join(scrt_tree, "Sessions"))}
    assert before == after


# --------------------------------------------------------------------------- #
# Global identity key (SecureCRT's "use global public key")
# --------------------------------------------------------------------------- #
def test_expand_vds_resolves_the_user_data_path(scrt_tree):
    got = scrt.expand_vds("${VDS_USER_DATA_PATH}/Documents/x.pk", scrt_tree)
    assert got == os.path.join(os.path.expanduser("~"), "Documents", "x.pk")


def test_expand_vds_resolves_the_ssh_data_path(scrt_tree):
    got = scrt.expand_vds("${VDS_SSH_DATA_PATH}/id_ed25519", scrt_tree)
    assert got == os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")


def test_expand_vds_leaves_unknown_variables_recognisable(scrt_tree):
    """Better a visibly unresolved path than a silently wrong one."""
    assert "VDS_MYSTERY" in scrt.expand_vds("${VDS_MYSTERY}/k.pem", scrt_tree)


def test_global_identity_key_is_read_from_ssh2_ini(scrt_tree):
    with open(os.path.join(scrt_tree, "SSH2.ini"), "w", encoding="utf-8") as fh:
        fh.write('S:"Identity Filename V2"=${VDS_USER_DATA_PATH}/Documents/identity.pk\n')
    got = scrt.global_identity_key(scrt_tree)
    assert got.endswith(os.path.join("Documents", "identity.pk"))
    assert "${" not in got


def test_global_identity_key_is_none_without_ssh2_ini(scrt_tree):
    assert scrt.global_identity_key(scrt_tree) is None


def test_sessions_using_the_global_key_record_no_key_path(scrt_tree):
    """Empty key_path == inherit, so the global key stays a single setting."""
    item = scrt.read_session_file(os.path.join(scrt_tree, "Sessions", "example.ini"))
    assert item.session.key_path is None


def test_a_session_overriding_the_global_key_keeps_its_own(tmp_path, scrt_tree):
    over = ('S:"Hostname"=edge.example.com\nS:"Username"=root\n'
            'D:"Use Global Public Key"=00000000\n'
            'S:"Identity Filename V2"=${VDS_SSH_DATA_PATH}/id_ed25519\n')
    p = os.path.join(scrt_tree, "Sessions", "override.ini")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(over)
    item = scrt.read_session_file(p)
    assert item.session.key_path.endswith(os.path.join(".ssh", "id_ed25519"))
