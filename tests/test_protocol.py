import struct

import pytest

from psyonic_ble import protocol as p


def test_grip_cmd():
    assert p.grip_cmd(3, 0.2) == "g03:0.200"
    assert p.grip_cmd(-1, 5.1234) == "g-1:5.123"
    assert p.grip_cmd(45, 1) == "g45:1.000"


def test_resolve_grip():
    assert p.resolve_grip("power") == 3
    assert p.resolve_grip("hang") == 17          # unique prefix
    assert p.resolve_grip("-1") == -1
    with pytest.raises(ValueError):
        p.resolve_grip("p")                     # ambiguous: pinch, power, point, peace, ...
    with pytest.raises(ValueError):
        p.resolve_grip(64)


def test_word_cmds():
    assert p.word_read_cmd(7, "float") == "Rm007"
    assert p.word_read_cmd(1, "hex") == "Rn001"
    assert p.word_write_cmd(1, "hex", "0x00120000") == "Wn001:0x00120000"
    assert p.word_write_cmd(8, "float", "1.5") == "Wm008:1.5"


def test_reply_parsers():
    assert p.parse_flag_reply("BIN16=0 BIN35=1") == {16: 0, 35: 1}
    assert p.parse_word_reply("mem[001]=0x00100000") == {1: "0x00100000"}


def test_decode_streams():
    pos = struct.pack("<6f", 1, 2, 3, 4, 5, -6)
    assert p.decode_stream(p.STREAMS["position"], pos) == [1, 2, 3, 4, 5, -6]
    packed = sum(v << (12 * i) for i, v in enumerate(range(30))).to_bytes(45, "little")
    assert p.decode_stream(p.STREAMS["fsr"], packed) == list(range(30))
    assert p.decode_stream(p.STREAMS["temperature"], b"32.1 33.0") == "32.1 33.0"
    assert p.decode_stream(p.STREAMS["position"], b"\x00\x01\x02") is None


def test_flag_tables_consistent():
    assert not set(p.FLAGS_BY_INDEX) & p.RESERVED_FLAGS
    assert p.resolve_flag("comm-uart") == 16 and p.resolve_flag("46") == 46


def test_hexcolor():
    assert p.hexcolor_cmd("#00fe45") == "CN00FE45"
    with pytest.raises(ValueError):
        p.hexcolor_cmd("red")
