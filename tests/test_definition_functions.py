"""Consumer headers compile on host/device and compute the parameterized IR expansions."""
import hashlib, random, re, struct, subprocess, sys, tempfile
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from functools import cache
import pytest
from sass import paths
from sass.assemble import assemble_instruction
from sass.emulate import build, definitions
from sass.semantics import bitvec, family, lifter
from sass.semantics.evaluate import evaluate
from tests.harness import definition_functions, gpu, native_full
from tests.test_emulate_fp import binary, half_boundaries, special


@family.definition("test.wide")
def wide(x):
    """Exercise operands and results above 64 bits."""
    return bitvec.concat(bitvec.extract(x, 63, 0), bitvec.extract(x, 127, 64))


@family.definition("test.many")
def many(x, others):
    """Five mixed-width arguments, including two independent 128-bit operands."""
    for other in others: x = x ^ bitvec.zext(other, 128)
    return x


@family.definition("test.mixed")
def mixed(x, y):
    """Encode a mixed-width tuple independently of the enumeration argument builder."""
    return bitvec.concat(y, x)


@family.definition("test.overload")
def overload(x, others=()):
    """Exercise operand-count overloads with the same storage types."""
    return x ^ others[0] if others else x


@family.definition("test.register")
def register(x):
    """A register is not a pure argument."""
    return x ^ bitvec.reg("R", 0, 1)


@family.definition("test.load")
def load(x):
    """A load is not a pure argument."""
    return bitvec.load("ram", x, 32)


@family.definition("test.primitive")
def primitive(x):
    """A runtime primitive is not a pure argument."""
    return bitvec.primitive("mufu_sin", 32, x)


@family.definition("test.unbound")
def unbound(x):
    """A captured parameter has no generated argument."""
    return x ^ bitvec.param("missing", 32)


@family.definition("test.toowide")
def toowide(x):
    """The C++ representation ends at 128 bits."""
    return bitvec.concat(x, x)


@family.definition("test.operandwide")
def operandwide(x):
    """A parameter alone can exceed the representation."""
    return x


@family.definition("test.nested")
def nested(x):
    """An expansion's named children must be fully inlined."""
    return overload(x, (bitvec.const(1, 32),))


@family.definition("test.selector")
def selector(x):
    """Unresolved decode-time selectors have no runtime representation."""
    return bitvec.Expr("sel", ("selector", ((0, x), (1, bitvec.const(1, 32)))), 32)


def uses_of(arch, text, fp="exact"):
    sem = lifter.lift(arch, assemble_instruction(arch, text), fp=fp)
    return definitions.definition_uses(definition_functions.roots(sem))


def test_collect_nested_and_deduplicate():
    uses = uses_of("SM89", "FMNMX.FTZ R0, R2, R3, PT")
    assert uses == {("ftz", (32,)), ("min.f32", (32, 32))}
    assert definitions.definition_uses([None, bitvec.const(1, 32)]) == set()
    assert definitions.definition_name("min.f32", (32, 32)) == "min_f32"
    header = definitions.definitions_header([*uses, *uses])
    assert header == definitions.definitions_header(reversed(sorted(uses)))
    assert header.count(" min_f32(") == header.count(" ftz(") == 1
    assert '#include "sass/fp.hpp"' in header


def test_registered_forms_and_identifiers():
    uses_of("SM89", "PRMT R0, R2, 0x3210, R3")
    assert definitions.definition_name("prmt.b32", (32, 32, 32)) == "prmt_b32"
    for label, widths, message in [
        ("test.unknown", (32,), "unregistered definition"),
        ("prmt.b32", (32,), "operand count"),
        ("prmt.b32", (32, 16, 32), "operand widths"),
        ("bad_label", (32,), "label"),
        ("0.bad", (), "C\\+\\+ function name"),
        ("class", (), "C\\+\\+ function name"),
        ("a..b", (), "C\\+\\+ function name"),
    ]:
        with pytest.raises(ValueError, match=message): definitions.definition_function(label, widths)


@pytest.mark.parametrize("fn, message", [
    (register, "reg"), (load, "load"), (primitive, "primitive"), (unbound, "missing"),
    (selector, "sel"),
])
def test_reject_impure_expansions(fn, message):
    expr = fn(bitvec.param("x", 32))
    with pytest.raises(NotImplementedError, match=f"{expr.op}.*{message}"):
        definitions.definition_function(expr.op, (32,))


def test_reject_unsupported_widths():
    expr = toowide(bitvec.param("x", 128))
    with pytest.raises(NotImplementedError, match="test.toowide.*256-bit"):
        definitions.definition_function(expr.op, (128,))
    operandwide(bitvec.param("x", 256))
    with pytest.raises(NotImplementedError, match="test.operandwide.*256-bit operand"):
        definitions.definition_function("test.operandwide", (256,))


def test_real_expansions_accept_parameters(real_uses):
    assert ("prmt.b32", (32, 32, 32)) in real_uses
    assert ("lut3", (8, 1, 1, 1)) in real_uses
    assert len(real_uses) > 100
    for label, widths in real_uses:
        assert re.fullmatch(r"[A-Za-z0-9.]+", label), (label, widths)
        params = [bitvec.param(f"a{k}", width) for k, width in enumerate(widths)]
        expansion = bitvec.lowered(bitvec.DEFINED[label].expand(*params))
        assert tuple(widths) == family.DEFINITIONS[label][1][len(widths)][2]
        assert 1 <= expansion.w <= 128
        allowed = set(params)
        for node in definitions.nodes([expansion]):
            assert node.op not in ("reg", "load", "primitive", "sel"), (label, node)
            assert node.op != "param" or node in allowed, (label, node)
            assert 1 <= node.w <= 128, (label, node)
        assert f"sass::Bits<{expansion.w}>" in definitions.definition_function(label, widths)


@pytest.mark.native
@pytest.mark.parametrize("backend", ("softfloat", "host"))
def test_generated_lut3_complete_domain(backend):
    use = ("lut3", (8, 1, 1, 1))
    assert use in uses_of("SM89", "PLOP3.LUT P0, P1, P2, P3, P4, 0xa8, 0x96")
    assert definitions.definition_name(*use) == "lut3"
    header = definitions.definitions_header([use])
    assert "sass::Bits<1> lut3(sass::Bits<8> a0," in header
    fn = definition_functions.compiled([use], backend)
    for table in range(256):
        for p, q, r in product((0, 1), repeat=3):
            assert definition_functions.call(fn, 0, (table, p, q, r)) == (
                table >> (4 * p + 2 * q + r) & 1)


@pytest.mark.gpu
def test_generated_lut3_cuda_complete_domain(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    use = ("lut3", (8, 1, 1, 1))
    assert use in uses_of(arch, "PLOP3.LUT P0, P1, P2, P3, P4, 0xa8, 0x96")
    source = definitions.definitions_header([use]).replace("#pragma once\n", "") + definition_functions.wrapper([use], device=True)
    data, want = [], []
    for table in range(256):
        for p, q, r in product((0, 1), repeat=3):
            data.extend((0, table, 0, p, 0, q, 0, r, 0))
            want.extend((table >> (4 * p + 2 * q + r) & 1, 0))
    fixture = SimpleNamespace(image=cuda_image(arch, source).read_bytes(), kernel="run",
                              data=struct.pack("<" + "Q" * len(data), *data), threads=len(want),
                              block=32, output_size=8, arguments=())
    driver = gpu.Driver(arch)
    try:
        assert driver.run(fixture) == want
    finally:
        driver.close()


@pytest.mark.parametrize("marker, count", [(None, 0), ("native", 0), ("native_full", 15)])
def test_full_suite_selection(marker, count):
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_native_full.py"]
    if marker: command.extend(["-m", marker])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == (0 if count else 5), result.stdout + result.stderr
    collected = [line for line in result.stdout.splitlines() if line.startswith("tests/test_native_full.py::")]
    assert len(collected) == count, result.stdout


@pytest.mark.native
def test_full_enumeration_counts_boundaries_and_mismatch():
    equal = native_full.Enumeration("boundary.RN", (3, 2), "sass::u128(index) << 96", "sass::u128(index) << 96")
    mismatch = native_full.Enumeration("mismatch.RZ", (3, 2), "sass::u128(index) << 96",
                                      "(sass::u128(index) << 96) ^ (sass::u128(index == 31) << 100)")
    empty = native_full.Enumeration("empty", (), "sass::u64(7)", "sass::u64(7)")
    mixed(bitvec.param("low", 3), bitvec.param("high", 2))
    use = ("test.mixed", (3, 2))
    encoded = native_full.Enumeration("encoding", (3, 2), native_full.defined(use).soft, "sass::u64(index)")
    fn = native_full.compiled([equal, mismatch, empty, encoded], [use])
    assert native_full.complete(fn, 0, equal)[0] == 32
    assert native_full.complete(fn, 2, empty)[0] == 1
    assert native_full.complete(fn, 3, encoded)[0] == 32
    assert equal.inputs(0) == (0, 0) and equal.inputs(31) == (7, 3)
    assert native_full.chunk(fn, 1, mismatch, 0, 31) == 31
    with pytest.raises(AssertionError, match=r"mismatch.RZ, signature \(3, 2\), index 0x1f, inputs"):
        native_full.chunk(fn, 1, mismatch, 31, 1)
    import ctypes
    out = (ctypes.c_uint64 * 5)()
    assert fn(0, 32, 0, out) == 0
    for start, count in ((32, 1), (33, 0), (0, native_full.CHUNK + 1)):
        assert fn(0, start, count, out) == bitvec.mask(64)


def test_nested_expansion_and_empty_header():
    expr = nested(bitvec.param("x", 32))
    assert definitions.definition_uses([expr, expr]) == {("test.nested", (32,))}
    source = definitions.definition_function("test.nested", (32,))
    assert "test_overload" not in source
    assert "test_nested" in source
    assert definitions.definitions_header([]).startswith('#pragma once\n#include "sass/fp.hpp"')


def extra_uses():
    wide(bitvec.param("x", 128))
    overload(bitvec.param("x", 32))
    overload(bitvec.param("x", 32), (bitvec.param("y", 32),))
    nested(bitvec.param("x", 32))
    many(bitvec.param("wide", 128), tuple(bitvec.param(f"other{k}", w) for k, w in enumerate((1, 32, 64, 128))))
    return [("test.wide", (128,)), ("test.overload", (32,)), ("test.overload", (32, 32)),
            ("test.nested", (32,)), ("test.many", (128, 1, 32, 64, 128))]


def vectors(widths):
    """Boundary rows, one-operand boundary variations, then deterministic random tuples."""
    edges = []
    for width in widths:
        values = [0, 1, bitvec.mask(width), 1 << (width - 1), (1 << (width - 1)) - 1]
        if width in (16, 32, 64): values.extend(special(width))
        edges.append(list(dict.fromkeys(values)))
    yield tuple(0 for _ in widths)
    for i in range(max(map(len, edges), default=0)):
        yield tuple(values[i % len(values)] for values in edges)
    for k, values in enumerate(edges):
        for value in values:
            row = [0] * len(widths); row[k] = value
            yield tuple(row)
    # Conversion midpoints and their immediate neighbours exercise all rounding modes.
    for width in (32, 64):
        if width not in widths: continue
        # The direct FP suite checks every selected midpoint. Keep this per-definition
        # corpus bounded while spanning zero/subnormal/normal/overflow transitions.
        boundaries = list(half_boundaries(width))
        for value in dict.fromkeys([*boundaries[:18], *boundaries[::64], *boundaries[-6:]]):
            yield tuple(value if w == width else 0 for w in widths)
        if widths.count(width) >= 2:
            first, second = [k for k, w in enumerate(widths) if w == width][:2]
            for a, b in binary(width, 710 + width, 16):
                row = [0] * len(widths)
                row[first], row[second] = a, b
                yield tuple(row)
    rng = random.Random(710)
    for _ in range(64): yield tuple(rng.getrandbits(width) for width in widths)


@cache
def expression(label, widths):
    params = [bitvec.param(f"a{k}", width) for k, width in enumerate(widths)]
    return bitvec.DEFINED[label].expand(*params)


def expected(label, widths, values):
    return evaluate(expression(label, widths), None, {f"a{k}": value for k, value in enumerate(values)})


@pytest.mark.native
@pytest.mark.parametrize("backend", ("softfloat", "host"))
def test_generated_functions(real_uses, backend):
    uses = sorted(set(real_uses + extra_uses()))
    for start in range(0, len(uses), definition_functions.BATCH):
        batch = uses[start:start + definition_functions.BATCH]
        fn = definition_functions.compiled(batch, backend)
        oracle = definition_functions.compiled(batch, "softfloat") if backend == "host" else None
        for i, (label, widths) in enumerate(batch):
            for values in vectors(widths):
                want = (definition_functions.call(oracle, i, values) if oracle else
                        expected(label, widths, values))
                assert definition_functions.call(fn, i, values) == want, (backend, label, widths, values)


@pytest.mark.gpu
def test_generated_functions_cuda(request, real_uses):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    uses = sorted(set(real_uses + extra_uses()))
    driver = gpu.Driver(arch)
    try:
        for start in range(0, len(uses), definition_functions.BATCH):
            batch = uses[start:start + definition_functions.BATCH]
            source = definitions.definitions_header(batch).replace("#pragma once\n", "") + definition_functions.wrapper(batch, device=True)
            image = cuda_image(arch, source)
            oracle = definition_functions.compiled(batch, "softfloat")
            stride = 1 + 2 * max(map(lambda use: len(use[1]), batch))
            data, want, rows = [], [], []
            for i, (label, widths) in enumerate(batch):
                for values in vectors(widths):
                    limbs = [limb for value in values for limb in (value & bitvec.mask(64), value >> 64)]
                    data.extend([i, *limbs, *([0] * (stride - 1 - len(limbs)))])
                    value = definition_functions.call(oracle, i, values)
                    want.extend((value & bitvec.mask(64), value >> 64))
                    rows.append((label, widths, values))
            padding = (-len(rows)) % 16
            data.extend(data[:stride] * padding)
            want.extend(want[:2] * padding)
            fixture = SimpleNamespace(image=image.read_bytes(), kernel="run",
                                      data=struct.pack("<" + "Q" * len(data), *data), threads=2 * (len(rows) + padding),
                                      block=32, output_size=8, arguments=())
            got = driver.run(fixture)
            assert len(got) == len(want)
            for i, row in enumerate(rows):
                assert got[2*i:2*i+2] == want[2*i:2*i+2], (arch, row, got[2*i:2*i+2], want[2*i:2*i+2])
    finally:
        driver.close()


def cuda_image(arch, source):
    flags = ["--allow-unsupported-compiler", "-std=c++20", "-ftz=false", "--fmad=false", "-cubin",
             f"-arch=sm_{arch[2:]}", "-I", str(build.INCLUDE)]
    version = subprocess.run([paths.NVCC, "--version"], capture_output=True, text=True, check=True).stdout
    key = hashlib.sha256(source.encode() + version.encode() + repr(flags).encode() +
                         b"".join(p.read_bytes() for p in build.SOURCES)).hexdigest()[:16]
    directory = paths.CACHE / "definition-functions"; directory.mkdir(parents=True, exist_ok=True)
    image = directory / f"{key}.cubin"
    if not image.exists():
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            cu, cubin = Path(tmp) / "definitions.cu", Path(tmp) / "definitions.cubin"
            cu.write_text(source)
            subprocess.run([paths.NVCC, *flags, str(cu), "-o", str(cubin)], check=True)
            cubin.replace(image)
    return image
