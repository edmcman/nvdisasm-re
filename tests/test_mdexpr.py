"""md expressions: operand sizes that control register dependencies, and CONDITIONS."""
import itertools
import pytest
from sass.decode import ir, pydecode
from sass.md import expression
from sass.decode import pyencode


@pytest.fixture(scope="module")
def md(): return ir.load("SM89")


def test_nested_ternary_arms(md):
    for values, expected in [({"a": 1, "b": 1}, 3), ({"a": 1, "b": 0}, 2), ({"a": 0, "c": 1}, 1), ({"a": 0, "c": 0}, 0)]:
        assert expression.evaluate("a ? b ? 3 : 2 : c ? 1 : 0", md, values) == expected


def test_mask_population_span(md):
    expr = "ceil((((mask & 1) + ((mask >> 1) & 1) + ((mask >> 2) & 1)) * 32)/32)"
    for mask in range(8): assert expression.evaluate(expr, md, {"mask": mask}) == mask.bit_count()


def test_enum_and_boolean(md):
    for flag, expected in ((0, 0), (1, 1)):
        assert expression.evaluate("r == `Register@RZ && !flag ? 0 : 1", md, {"r": 255, "flag": flag}) == expected


def test_dotted_operand(md):
    assert expression.free_vars(md, "size.ext ? 2 : 1") == {"size.ext"}
    assert expression.evaluate("size.ext ? 2 : 1", md, {"size.ext": 1}) == 2


# ---- CONDITIONS: which encodings are md-illegal ----

def condition(md, cls, message):
    return next(e for _, e, m in next(k for k in md.classes if k.name == cls).conditions if message in m)


def test_attribute_implication(md):
    expr = condition(md, "iadd3_noimm__RRR_RRR", "nA-Rb")  # Ra@negate -> !Rb@negate
    for a, b in itertools.product((0, 1), repeat=2):
        assert expression.holds(md, expr, {("Ra", "negate"): a, ("Rb", "negate"): b}) == (not (a and b))


def test_register_range_parameter(md):
    expr = condition(md, "iadd3_noimm__RRR_RRR", "Register Rd is out of range")  # %MAX_REG_COUNT
    assert [r for r in range(256) if not expression.holds(md, expr, {"Rd": r})] == [254]


def test_table_lookup_and_defined(md):
    k = next(k for k in md.classes if any("TABLES_mem_0(" in e for _, e, _ in k.conditions))
    expr = next(e for _, e, _ in k.conditions if "TABLES_mem_0(" in e)
    key, out = next((key, out) for key, out in expression.table_rows(md.name, "TABLES_mem_0") if None not in key)
    env = dict(zip(("sem", "sco", "private"), key))
    assert expression.holds(md, expr, env) == (out not in (10, 12))
    assert not expression.holds(md, expr, {"sem": -1, "sco": -1, "private": -1})  # no row: not DEFINED


def test_strict_decoding(md):
    cls = "iadd3_noimm__RRR_RRR"
    legal = bytes.fromhex(pyencode.encode("SM89", cls, Rd=4, Ra=2, Rb=6, Rc="RZ"))
    negate = sum(f.mask for f, r in next(k for k in md.classes if k.name == cls).rules if r in (("attr", "Ra", "negate"), ("attr", "Rb", "negate")))
    illegal = (int.from_bytes(legal, "little") | negate).to_bytes(16, "little")
    assert pydecode.decode("SM89", legal).klass.name == cls
    with pytest.raises(pydecode.NoMatch, match="nA-Rb|Ra-nB"): pydecode.decode("SM89", illegal)
    assert pydecode.decode("SM89", illegal, conditions=False).text.startswith("IADD3 R4, -R2, -R6")
