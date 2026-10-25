"""The independent references stay independent of the generated semantics, and their
building blocks agree with hand values and brute force."""
import ast
from fractions import Fraction
from pathlib import Path
import pytest
from tests.refs import fpformat as fpf
from tests.refs.integer import bmsk, dot4, i2ip, prmt, isetp, shf

REFS = Path(__file__).parent / "refs"


def test_dsetp_minmax_total_order():
    from tests.refs.float import dsetp_compare
    one, two, nan, neg_nan = 0x3ff0 << 48, 0x4000 << 48, 0x7ff8 << 48, 0xfff8 << 48
    # (a, c, MIN, MAX)
    for a, c, minimum, maximum in [(one, two, 1, 0), (two, one, 0, 1), (one, one, 1, 0), (0, 1 << 63, 0, 1), (1 << 63, 0, 1, 0), (1, 0, 0, 1),
                                   (0xbff0 << 48, 0, 1, 0), (nan, one, 0, 0), (nan, nan, 0, 0), (neg_nan, one, 0, 0), (one, nan, 1, 1), (one, neg_nan, 1, 1)]:
        assert dsetp_compare(a, c, 'MIN') == bool(minimum) and dsetp_compare(a, c, 'MAX') == bool(maximum)


def test_divergence_branch_observations():
    from tests.refs.control import divergence_branch
    # (cond, mask, waiting lanes, Pp, taken) from research/bra_div/observations.json (SM89)
    for cond, mask, waiting, pp, taken in [("DIV", 0xffffffff, 0, 1, 0), ("CONV", 0xffffffff, 0, 1, 1), ("DIV", 0xffffffff, 0xffff0000, 1, 1),
                                           ("CONV", 0x0000ff00, 0xffff0000, 1, 1), ("CONV", 0x00ffff00, 0xffff0000, 1, 0), ("DIV", 0, 0xffff0000, 1, 0),
                                           ("DIV", 0x3, 0xfffffffe, 1, 1), ("CONV", 0xffffffff, 0, 0, 0), ("DIV", 0xffffffff, 0xffff0000, 0, 0)]:
        assert divergence_branch(cond, mask, waiting, pp) == bool(taken)


def test_double_comparison_masks():
    from tests.refs.float import fcompare
    # The comparison nibble selects LT, EQ, GT and unordered outcome bits.
    labels = ('MIN','LT','EQ','LE','GT','NE','GE','NUM','NAN','LTU','EQU','LEU','GTU','NEU','GEU','MAX')
    for code, label in enumerate(labels):
        if label in ('MIN', 'MAX'): continue
        for a, b, outcome in [(1,2,1),(0.0,-0.0,2),(2,1,4),(None,0,8),(0,None,8),(None,None,8)]:
            assert fcompare(a,b,label) == bool(code & outcome)


def test_isetp_extended_reference():
    assert isetp(7, 7, 'LT', 'AND', True, low=True) == (1, 0)
    assert isetp(7, 7, 'LT', 'AND', True, low=False) == (0, 1)
    assert isetp(7, 7, 'F', 'OR', False, low=True) == (0, 1)
    assert isetp(7, 7, 'T', 'XOR', True, low=False) == (0, 1)
    assert isetp(0xffffffff, 0, 'LT', 'AND', True, signed=True) == (1, 0)
    assert isetp(0xffffffff, 0, 'LT', 'AND', True) == (0, 1)


@pytest.mark.parametrize('fmt', ['U32', 'S32', 'U64', 'S64'])
@pytest.mark.parametrize('direction', ['L', 'R'])
@pytest.mark.parametrize('mode', ['C', 'W'])
@pytest.mark.parametrize('half', ['LO', 'HI'])
def test_shf_reference_matches_integer_bit_string(fmt, direction, mode, half):
    for a, c in ((0, 0), (0xffffffff, 0x80000000), (0x01234567, 0x89abcdef)):
        for shift in (0, 1, 31, 32, 33, 63, 64, 65, 0xffffffff):
            width = int(fmt[1:])
            count = min(shift, width) if mode == 'C' else shift % width
            data = (c << 32) | a
            if fmt.startswith('S') and data >> 63: data -= 1 << 64
            shifted = data >> count if direction == 'R' else data << count
            expected = (shifted >> (32 if half == 'HI' else 0)) & 0xffffffff
            assert shf(a, shift, c, fmt, direction, mode, half) == expected


@pytest.mark.parametrize("path", sorted(REFS.glob("*.py")), ids=lambda p: p.name)
def test_refs_import_only_softfloat_from_sass(path):
    imports = {node.module for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.module}
    imports |= {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Import) for alias in node.names}
    assert all(m == "sass.semantics.fp" or not m.startswith("sass") for m in imports), sorted(imports)
    names = {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.module == "sass.semantics.fp" for alias in node.names}
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


@pytest.mark.parametrize('mode,selector,result', [
    ('IDX', 0x6420, 0x06040200), ('F4E', 1, 0x04030201),
    ('B4E', 1, 0x06070001), ('RC8', 2, 0x02020202),
    ('RC16', 1, 0x03020302), ('ECL', 2, 0x03020202),
    ('ECR', 2, 0x02020100),
])
def test_permutation_hand_calculated_examples(mode, selector, result):
    assert prmt(0x03020100, selector, 0x07060504, mode) == result


@pytest.mark.parametrize('value,result', [(0,0), (1,1), (0xffffffff,1),
    (0x7fffffff,0x7fffffff), (0x80000000,0x80000000), (0x80000001,0x7fffffff)])
def test_integer_absolute_boundaries(value, result):
    from tests.refs import integer
    assert integer.iabs(value) == result


def test_move_preserves_high_word():
    from tests.refs import integer
    assert integer.mov(0x8123456789abcdef, 64) == 0x8123456789abcdef
    assert integer.mov(0x8123456789abcdef, 32) == 0x89abcdef


def test_lop3_complete_bit_sliced_truth_tables():
    from tests.refs.integer import lop3
    # Each byte contains all eight Boolean input combinations, in LUT-bit order.
    for table in range(256):
        result, predicate = lop3(0xf0f0f0f0, 0xcccccccc, 0xaaaaaaaa, table, 0, 'POR')
        assert result == table * 0x01010101
        assert predicate == int(table != 0)
    for table, nonzero in ((0, 0), (0xff, 1)):
        for pred in (0, 1):
            assert lop3(0, 0, 0, table, pred, 'POR')[1] == (nonzero | pred)
            assert lop3(0, 0, 0, table, pred, 'PAND')[1] == (nonzero & pred)


def test_arithmetic_carry_boundaries():
    from tests.refs.integer import iadd3, lea, imad
    assert iadd3(0,0,0,(True,False,False)) == (0,1,0)
    assert iadd3(0xffffffff,0xffffffff,0xffffffff) == (0xfffffffd,1,1)
    assert iadd3(0,0,0,(True,False,False),True,1,0) == (0,1,0)
    assert lea(0,0,0,0,modifications=(True,False)) == (0,1)
    assert lea(1,0,1,31,high=True) == (0x80000000,0)
    assert imad(0,0,0xffffffff,'HI',False,carry_in=1,addend_width=32) == (0,1)


def test_viaddmnmx_negates_each_packed_lane():
    from tests.refs.integer import viaddmnmx
    # SM90 register, constant and CX observations: 0 - 1 has no borrow into lane 1.
    for signed in (False, True):
        assert viaddmnmx(0, 1, 0x7fffffff, 16, signed, True, negate_b=True) == 0x0000ffff
    assert viaddmnmx(0, 1, 0x7fffffff, 32, False, True, negate_b=True) == 0x7fffffff
    assert viaddmnmx(0, 1, 0x7fffffff, 32, True, True, negate_b=True) == 0xffffffff
