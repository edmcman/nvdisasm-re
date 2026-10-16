"""Bitvector construction and slicing, checked against explicit bit strings."""
import pytest
from sass.semantics import bitvec
from sass.semantics.evaluate import evaluate


@pytest.mark.parametrize("widths", [(8, 8), (3, 5, 1, 7), (17, 31, 16), (65, 7, 64), (1,) * 128])
def test_concat(widths):
    parts = [bitvec.param(f"p{j}", w) for j, w in enumerate(widths)]
    e = bitvec.concat(*parts)
    assert e.op == "concat"
    assert len(e.args) == len(parts)
    assert all(x is y for x, y in zip(e.args, parts))
    assert e.w == sum(widths)
    assert bitvec.concat(*parts) is e
    for values in ([0] * len(parts), [bitvec.mask(w) for w in widths],
                   [bitvec.mask(w) // 3 + j % 2 for j, w in enumerate(widths)]):
        bits = ''.join(f"{v:0{w}b}" for v, w in zip(values, widths))
        want = int(bits, 2)
        assert evaluate(e, None, dict(zip((p.args[0] for p in parts), values))) == want
        folded = bitvec.concat(*(bitvec.const(v, w) for v, w in zip(values, widths)))
        assert bitvec.is_const(folded, want)
        assert folded.w == e.w


def test_concat_arity_and_nesting():
    a, b, c = (bitvec.param(k, 8) for k in ('a', 'b', 'c'))
    assert bitvec.concat(a) is a
    with pytest.raises(TypeError, match="at least one"):
        bitvec.concat()
    inner = bitvec.concat(a, b)
    e = bitvec.concat(inner, c, inner)
    assert e.args[0] is inner and e.args[2] is inner
    assert evaluate(e, None, dict(a=0x12, b=0x34, c=0x56)) == 0x1234561234
    assert repr(bitvec.concat(a, b, c)) == 'concat(a, b, c)'


def test_extract_concat():
    widths, values = (3, 5, 2, 7), (5, 19, 2, 101)
    parts = [bitvec.param(f"p{j}", w) for j, w in enumerate(widths)]
    e = bitvec.concat(*parts)
    params = {p.args[0]: v for p, v in zip(parts, values)}
    bits = ''.join(f"{v:0{w}b}" for v, w in zip(values, widths))
    for lo in range(e.w):
        for hi in range(lo, e.w):
            sliced = bitvec.extract(e, hi, lo)
            assert evaluate(sliced, None, params) == int(bits[e.w - hi - 1:e.w - lo], 2)
    offset = 0
    for part in reversed(parts):
        assert bitvec.extract(e, offset + part.w - 1, offset) is part
        if part.w > 1:
            assert bitvec.extract(e, offset + part.w - 2, offset) is bitvec.extract(part, part.w - 2, 0)
        offset += part.w
    assert bitvec.extract(e, 8, 6).op == 'extract'  # crosses the low two operands
    nested = bitvec.concat(parts[0], bitvec.concat(*parts[1:]))
    assert bitvec.extract(nested, 13, 9) is parts[1]
