"""Key generation / inspection (no server needed)."""

import os

import pytest

from sshdeck import keys
from sshdeck.errors import SSHDeckError


def test_generate_ed25519_and_pubkey(tmp_path):
    path = str(tmp_path / "id_ed25519")
    keys.generate_keypair(type="ed25519", path=path)
    assert os.path.exists(path)
    assert os.path.exists(path + ".pub")

    # Public string from the .pub file and from loading the private key agree.
    pub_from_file = keys.public_key_string(path + ".pub")
    pub_from_priv = keys.public_key_string(path)
    assert pub_from_file.startswith("ssh-ed25519 ")
    assert pub_from_priv.startswith("ssh-ed25519 ")
    assert pub_from_file == pub_from_priv

    # And it loads back as a real key.
    loaded = keys.load_key(path)
    assert loaded.get_name() == "ssh-ed25519"


def test_generate_rsa(tmp_path):
    path = str(tmp_path / "id_rsa")
    keys.generate_keypair(type="rsa", path=path, bits=2048)
    pub = keys.public_key_string(path)
    assert pub.startswith("ssh-rsa ")
    assert keys.load_key(path).get_name() == "ssh-rsa"


def test_passphrase_protected_key(tmp_path):
    path = str(tmp_path / "id_enc")
    keys.generate_keypair(type="ed25519", path=path, passphrase="s3cret")
    # Wrong / missing passphrase -> clean error.
    with pytest.raises(SSHDeckError):
        keys.load_key(path)
    # Correct passphrase loads.
    key = keys.load_key(path, passphrase="s3cret")
    assert key.get_name() == "ssh-ed25519"


def test_comment_is_appended(tmp_path):
    path = str(tmp_path / "id_ed25519")
    keys.generate_keypair(type="ed25519", path=path, comment="me@box")
    assert keys.public_key_string(path + ".pub").endswith(" me@box")


def test_bad_type_and_missing_file(tmp_path):
    with pytest.raises(SSHDeckError):
        keys.generate_keypair(type="magic", path=str(tmp_path / "x"))
    with pytest.raises(SSHDeckError):
        keys.load_key(str(tmp_path / "does-not-exist"))
