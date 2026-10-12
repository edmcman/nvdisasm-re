"""Layer 0: the independent references stay independent of the generated semantics, and their
building blocks agree with hand values and brute force."""
import ast
from fractions import Fraction
from pathlib import Path
import pytest
from tests.refs import fpformat as fpf
from tests.refs.integer import bmsk, dot4, i2ip, prmt

REFS = Path(__file__).parent / "refs"


@pytest.mark.parametrize("path", sorted(REFS.glob("*.py")), ids=lambda p: p.name)
def test_refs_import_only_softfloat_from_sass(path):
    imports = {node.module for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.module}
    imports |= {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Import) for alias in node.names}
    assert all(m == "sass" or not m.startswith("sass") for m in imports), sorted(imports)
    names = {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.module == "sass" for alias in node.names}
    assert names <= {"softfloat"}, names


@pytest.mark.parametrize("fmt,code,value", [("E4M3", 0x7e, 448), ("E4M3", 1, Fraction(1, 512)), ("E5M2", 0x7b, 57344),
                                            ("E2M3", 0x1f, Fraction(15, 2)), ("E3M2", 0x1f, 28), ("E2M1", 7, 6), ("E0M3", 7, 7),
                                            ("E3M4", 0x7e, 30), ("E6M9", 0x7dff, 1023 * 2 ** 22)])
def test_max_and_subnormal_values(fmt, code, value):
    assert fpf.decode(fmt, code) == ("finite", False, value)


@pytest.mark.parametrize("fmt", ["E4M3", "E5M2", "E2M3", "E3M2", "E2M1", "E0M3"])
def test_rounding_matches_brute_force(fmt):
    """Bisection rounding equals nearest-even/floor/ceil over every encoding."""
    codes = fpf.finite(fmt); high = codes[-1][1]
    probes = sorted({v for _, v in codes} | {(a[1] + b[1]) / 2 for a, b in zip(codes, codes[1:])} | {v * Fraction(3, 4) for _, v in codes})
    for mag in (p for p in probes if p <= high):
        best = min(abs(mag - v) for _, v in codes)
        tied = [c for c, v in codes if abs(mag - v) == best]
        assert fpf.round_magnitude(fmt, mag, "RN")[0] == next((c for c in tied if not c & 1), tied[0])
        assert fpf.round_magnitude(fmt, mag, "RZ")[0] == max(c for c, v in codes if v <= mag)
        assert fpf.round_magnitude(fmt, mag, "RP")[0] == min(c for c, v in codes if v >= mag)


def test_conversion_rules():
    assert fpf.fp8(0x7f800000, "E4M3") == 0x7e and fpf.fp8(0x7f800000, "E4M3", satfinite=False) == 0x7f
    assert fpf.fp8(0xff800000, "E5M2", satfinite=False) == 0xfc and fpf.fp8(0xffc00000, "E4M3") == 0xff
    assert fpf.f32_to_f16(0x7f7fffff, "RZ") == 0x7bff and fpf.f32_to_f16(0x7f7fffff) == 0x7c00
    assert fpf.f32_to_bf16(0x3f808000) == 0x3f80 and fpf.f32_to_bf16(0x3f818000) == 0x3f82
    assert fpf.widen("E5M2", 0x7d) == 0x7fff and fpf.widen("E4M3", 0x08) == 0x2400
    assert fpf.s2_6(0x3f800000, 127) == 64 and fpf.s2_6(0x7f800000, 127) == 127 and fpf.s2_6(0x7fc00000, 0) == 0x7f


def test_integer_rules():
    assert bmsk(4, 4, "C") == 0xf0 and bmsk(31, 33, "W") == 0x80000000 and bmsk(0, 33, "C") == 0xffffffff
    assert dot4(0x01020304, 0x05060708, 10, False, False) == 10 + 5 + 12 + 21 + 32
    assert prmt(0x03020100, 0x6420, 0x07060504, "IDX") == 0x06040200
    assert i2ip(300, -140, 0x12345678, 8, True, True, False, "H1") == 0x12347f80
