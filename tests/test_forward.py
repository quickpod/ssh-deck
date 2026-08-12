"""Port-forward spec parsing/description (pure, no server needed)."""

import pytest

from sshdeck import forward
from sshdeck.errors import SSHDeckError


def test_three_field_spec_defaults_bind():
    fwd = forward.parse_forward("8080:localhost:80")
    assert fwd.kind == "local"
    assert fwd.bind_host == "127.0.0.1"
    assert fwd.bind_port == 8080
    assert fwd.dest_host == "localhost"
    assert fwd.dest_port == 80


def test_four_field_spec():
    fwd = forward.parse_forward("0.0.0.0:5432:db.internal:5432", kind="remote")
    assert fwd.kind == "remote"
    assert fwd.bind_host == "0.0.0.0"
    assert fwd.bind_port == 5432
    assert fwd.dest_host == "db.internal"
    assert fwd.dest_port == 5432


def test_describe_mentions_endpoints():
    text = forward.describe("8080:localhost:80")
    assert "8080" in text and "localhost:80" in text


def test_roundtrip_equdict():
    fwd = forward.parse_forward("127.0.0.1:9000:svc:9000")
    assert forward.ForwardSpec(**fwd.to_dict()) == fwd


@pytest.mark.parametrize("bad", [
    "",                     # empty
    "80",                   # too few fields
    "80:host",              # too few fields
    "a:b:c:d:e",            # too many fields
    "notaport:host:80",     # non-numeric bind port
    "80:host:notaport",     # non-numeric dest port
    "0:host:80",            # port out of range
    "80:host:99999",        # port out of range
    "80::80",               # missing dest host
])
def test_bad_specs_raise(bad):
    with pytest.raises(SSHDeckError):
        forward.parse_forward(bad)


def test_bad_kind_raises():
    with pytest.raises(SSHDeckError):
        forward.parse_forward("8080:localhost:80", kind="sideways")
