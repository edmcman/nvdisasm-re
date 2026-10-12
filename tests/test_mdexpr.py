"""Layer 0: md size expressions that control register dependencies."""
import pytest
from sass import ir, mdexpr


@pytest.fixture(scope="module")
def md(): return ir.load("SM89")


def test_nested_ternary_arms(md):
    for values, expected in [({"a": 1, "b": 1}, 3), ({"a": 1, "b": 0}, 2), ({"a": 0, "c": 1}, 1), ({"a": 0, "c": 0}, 0)]:
        assert mdexpr.evaluate("a ? b ? 3 : 2 : c ? 1 : 0", md, values) == expected


def test_mask_population_span(md):
    expr = "ceil((((mask & 1) + ((mask >> 1) & 1) + ((mask >> 2) & 1)) * 32)/32)"
    for mask in range(8): assert mdexpr.evaluate(expr, md, {"mask": mask}) == mask.bit_count()


def test_enum_and_boolean(md):
    for flag, expected in ((0, 0), (1, 1)):
        assert mdexpr.evaluate("r == `Register@RZ && !flag ? 0 : 1", md, {"r": 255, "flag": flag}) == expected


def test_dotted_operand(md):
    assert mdexpr.free_vars(md, "size.ext ? 2 : 1") == {"size.ext"}
    assert mdexpr.evaluate("size.ext ? 2 : 1", md, {"size.ext": 1}) == 2
