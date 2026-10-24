"""The staging rewrite of families and helpers (family.rewrite)."""
import textwrap, types, pytest
from sass.semantics.bitvec import param
from sass.semantics.family import helper


def staged(tmp_path, source):
    """A helper `f` from module-level source in a file (rewrite reads the source back)."""
    path = tmp_path / "staged.py"
    path.write_text("from sass.semantics.bitvec import *\n" + textwrap.dedent(source))
    module = types.ModuleType("staged")
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return helper(module.f)


@pytest.mark.parametrize("source", [
    "def f(x): return z if (z := x) == 1 else z + 1",          # a conditional's test runs first
    "def f(x): return (z := x == 1) and z",                     # so does the first and/or operand
])
def test_walrus_evaluated_first_is_kept(tmp_path, source):
    assert staged(tmp_path, source)(param("x", 32)) is not None


@pytest.mark.parametrize("source", [
    "def f(x): return (y := x + 1) if x == 1 else x",          # a conditional's branch
    "def f(x): return x if x == 1 else (y := x)",
    "def f(x): return (x == 1) or (y := x == 2)",              # a later and/or operand
    "def f(x): return (x == 1) and (x == 2) and (y := x == 3)",
])
def test_walrus_in_a_deferred_operand_is_rejected(tmp_path, source):
    with pytest.raises(SyntaxError, match=r"binds inside the staged lambda \(line 2\)"): staged(tmp_path, source)
