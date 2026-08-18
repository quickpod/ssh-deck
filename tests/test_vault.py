r"""Encrypted profile/credential vault.

Pure crypto: no network, no GUI, no keyring. The cost parameters are lowered
where a test does many derivations, so the suite stays fast without changing
the code path under test.
"""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from sshdeck import vault
from sshdeck.vault import VaultError, WrongPassphrase

pytestmark = pytest.mark.skipif(not vault.crypto_available(),
                                reason="cryptography not installed")

SAMPLE = {"sessions": [{"name": "example", "host": "192.0.2.10", "user": "root"}],
          "credentials": [{"title": "fleet", "password": "hunter2"}]}
PW = "correct horse battery staple"


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_seal_open_roundtrip():
    assert vault.open_envelope(vault.seal(SAMPLE, PW), PW) == SAMPLE


def test_wrong_passphrase_is_rejected():
    env = vault.seal(SAMPLE, PW)
    with pytest.raises(WrongPassphrase):
        vault.open_envelope(env, PW + "!")


def test_plaintext_never_appears_in_the_envelope():
    """The whole point: no secret is recoverable without the passphrase."""
    blob = json.dumps(vault.seal(SAMPLE, PW))
    assert "hunter2" not in blob
    assert "192.0.2.10" not in blob
    assert "example" not in blob


def test_passphrase_is_not_stored_anywhere():
    env = vault.seal(SAMPLE, PW)
    blob = json.dumps(env)
    assert PW not in blob
    # nor any hash of it that could be verified offline
    assert "hash" not in {k.lower() for k in env}


def test_same_input_seals_differently_each_time():
    """Fresh salt + nonce per seal, so identical stores are not correlatable."""
    a, b = vault.seal(SAMPLE, PW), vault.seal(SAMPLE, PW)
    assert a["salt"] != b["salt"]
    assert a["nonce"] != b["nonce"]
    assert a["payload"] != b["payload"]


# --------------------------------------------------------------------------- #
# Tamper resistance
# --------------------------------------------------------------------------- #
def test_flipping_a_ciphertext_bit_is_detected():
    env = vault.seal(SAMPLE, PW)
    raw = bytearray(base64.b64decode(env["payload"]))
    raw[0] ^= 0x01
    env["payload"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(WrongPassphrase):
        vault.open_envelope(env, PW)


def test_header_is_authenticated_so_the_kdf_cannot_be_downgraded():
    """The header is AEAD associated data -- editing it must break the tag."""
    env = vault.seal(SAMPLE, PW)
    if env["kdf"]["name"] != "argon2id":
        pytest.skip("argon2id not in use on this build")
    env["kdf"] = {"name": "scrypt", "n": 2, "r": 1, "p": 1}   # far cheaper
    with pytest.raises((WrongPassphrase, VaultError)):
        vault.open_envelope(env, PW)


def test_swapping_the_salt_is_detected():
    a, b = vault.seal(SAMPLE, PW), vault.seal(SAMPLE, PW)
    a["salt"] = b["salt"]
    with pytest.raises(WrongPassphrase):
        vault.open_envelope(a, PW)


def test_foreign_json_is_not_mistaken_for_a_vault():
    with pytest.raises(VaultError):
        vault.open_envelope({"hello": "world"}, PW)


def test_future_format_version_is_refused_clearly():
    env = vault.seal(SAMPLE, PW)
    env["version"] = vault.VERSION + 1
    with pytest.raises(VaultError) as exc:
        vault.open_envelope(env, PW)
    assert "newer" in str(exc.value).lower()


def test_empty_passphrase_is_refused():
    with pytest.raises(VaultError):
        vault.seal(SAMPLE, "")


# --------------------------------------------------------------------------- #
# On-disk behaviour
# --------------------------------------------------------------------------- #
def test_write_read_file_roundtrip(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    assert vault.read_vault(p, PW) == SAMPLE
    assert vault.is_vault_file(p) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_vault_file_is_private_to_the_user(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    vault.write_vault(p, SAMPLE, PW)
    assert [f.name for f in tmp_path.iterdir()] == ["vault.json"]


def test_plain_json_is_not_reported_as_a_vault(tmp_path):
    p = tmp_path / "sessions.json"
    p.write_text('[{"name": "example"}]', encoding="utf-8")
    assert vault.is_vault_file(str(p)) is False


def test_missing_file_reports_clearly(tmp_path):
    with pytest.raises(VaultError):
        vault.read_vault(str(tmp_path / "nope.json"), PW)


def test_damaged_file_reports_clearly(tmp_path):
    p = tmp_path / "vault.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(VaultError):
        vault.read_vault(str(p), PW)


# --------------------------------------------------------------------------- #
# Changing the passphrase
# --------------------------------------------------------------------------- #
def test_change_passphrase_reseals_under_the_new_one(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    vault.change_passphrase(p, PW, "a new passphrase")
    assert vault.read_vault(p, "a new passphrase") == SAMPLE
    with pytest.raises(WrongPassphrase):
        vault.read_vault(p, PW)


def test_change_passphrase_needs_the_old_one(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    with pytest.raises(WrongPassphrase):
        vault.change_passphrase(p, "not it", "new")
    assert vault.read_vault(p, PW) == SAMPLE      # left untouched


def test_change_passphrase_uses_fresh_key_material(tmp_path):
    p = str(tmp_path / "vault.json")
    vault.write_vault(p, SAMPLE, PW)
    before = json.loads(open(p).read())
    vault.change_passphrase(p, PW, "another one")
    after = json.loads(open(p).read())
    assert before["salt"] != after["salt"]
    assert before["nonce"] != after["nonce"]


# --------------------------------------------------------------------------- #
# Password hint — readable without the passphrase, by design
# --------------------------------------------------------------------------- #
def test_hint_survives_a_round_trip(tmp_path):
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="the usual one")
    assert vault.read_hint(p) == "the usual one"
    assert vault.read_vault(p, PW) == SAMPLE


def test_hint_is_readable_without_the_passphrase(tmp_path):
    """The whole point: someone locked out must still see their reminder."""
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="my reminder")
    with pytest.raises(WrongPassphrase):
        vault.read_vault(p, "wrong")
    assert vault.read_hint(p) == "my reminder"      # still legible


def test_hint_cannot_be_altered_without_breaking_the_tag(tmp_path):
    """It is plaintext but authenticated -- readable, not editable."""
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="original")
    env = json.load(open(p))
    env["hint"] = "tampered"
    with open(p, "w") as fh:
        json.dump(env, fh)
    with pytest.raises(WrongPassphrase):
        vault.read_vault(p, PW)


def test_no_hint_means_empty_not_an_error(tmp_path):
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW)
    assert vault.read_hint(p) == ""


def test_hint_of_a_missing_or_foreign_file_is_empty(tmp_path):
    assert vault.read_hint(str(tmp_path / "nope.json")) == ""
    plain = tmp_path / "plain.json"
    plain.write_text('{"not": "a vault"}', encoding="utf-8")
    assert vault.read_hint(str(plain)) == ""


def test_changing_the_passphrase_keeps_the_hint(tmp_path):
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="keep me")
    vault.change_passphrase(p, PW, "another one")
    assert vault.read_hint(p) == "keep me"
    assert vault.read_vault(p, "another one") == SAMPLE


def test_changing_the_passphrase_can_replace_the_hint(tmp_path):
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="old hint")
    vault.change_passphrase(p, PW, "another one", hint="new hint")
    assert vault.read_hint(p) == "new hint"


def test_the_hint_never_leaks_the_contents(tmp_path):
    p = str(tmp_path / "v.json")
    vault.write_vault(p, SAMPLE, PW, hint="a reminder")
    blob = open(p).read()
    assert "hunter2" not in blob and "192.0.2.10" not in blob
