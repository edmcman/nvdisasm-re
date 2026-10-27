"""Consumer headers compile on host/device and compute the parameterized IR expansions."""
import ctypes, hashlib, itertools, random, struct, subprocess, tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from sass import paths
from sass.assemble import assemble_instruction
from sass.emulate import build, definitions
from sass.semantics import bitvec, family, lifter
from sass.semantics.evaluate import evaluate
from tests import cases
from tests.harness import gpu
from tests.test_emulate_fp import special


@family.definition("test.wide")
def wide(x):
    """Exercise operands and results above 64 bits."""
    return bitvec.concat(bitvec.extract(x, 63, 0), bitvec.extract(x, 127, 64))


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


def roots(semantics):
    out = [semantics.guard, *(value for _, value in semantics.writes)]
    for store in semantics.stores: out.extend((store.cond, store.address, store.value))
    for effect in semantics.effects: out.extend(effect.args)
    if semantics.flow: out.extend((semantics.flow.cond, semantics.flow.target))
    return out


def uses_of(arch, text, fp="exact"):
    return definitions.definition_uses(roots(lifter.lift(arch, assemble_instruction(arch, text), fp=fp)))


@pytest.fixture(scope="module")
def real_uses():
    """Manual shared cases: deduplicate words before lifting, without generated-plan work."""
    uses = set()
    for arch, fp in itertools.product(("SM75", "SM80", "SM89", "SM90"), ("exact", "readable")):
        words = set()
        for fam in cases.FAMILIES:
            for case in cases.stream(arch, fp, fam, 0, None):
                word = case.request.get("word")
                if word is None or word in words: continue
                words.add(word)
                semantics = lifter.lift(arch, bytes.fromhex(word), fp=fp, conditions=False)
                uses.update(definitions.definition_uses(roots(semantics)))
    return sorted(uses)


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
    for label, widths in real_uses:
        assert "SASS_HD inline" in definitions.definition_function(label, widths)


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
    return [("test.wide", (128,)), ("test.overload", (32,)), ("test.overload", (32, 32)),
            ("test.nested", (32,))]


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
    rng = random.Random(710)
    for _ in range(64): yield tuple(rng.getrandbits(width) for width in widths)


def wrapper(uses, device=False):
    """One dispatch function; every operand/result travels as two u64 limbs."""
    head = ('extern "C" __global__ void run(const sass::u64 *input, sass::u64 *output) {\n'
            '  const unsigned thread = blockIdx.x * blockDim.x + threadIdx.x;\n'
            '  const sass::u64 *a = input + (thread / 2) * 8;\n'
            '  sass::u64 *out = output + thread;\n'
            '  const unsigned which = a[6];') if device else (
            'extern "C" void run(unsigned which, const sass::u64 *a, sass::u64 *out) {')
    lines = [head, "  switch (which) {"]
    for i, (label, widths) in enumerate(uses):
        args = [f"sass::Bits<{width}>(a[{2*k}])" if width <= 64 else
                f"(sass::u128(a[{2*k}]) | sass::u128(a[{2*k+1}]) << 64)"
                for k, width in enumerate(widths)]
        name = definitions.definition_name(label, widths)
        outputs = ["      out[0] = thread % 2 ? sass::u64(sass::u128(value) >> 64) : sass::u64(value);"] if device else [
            "      out[0] = sass::u64(value);", "      out[1] = sass::u64(sass::u128(value) >> 64);"]
        lines.extend([f"    case {i}: {{", f"      auto value = sass::def::{name}({', '.join(args)});",
                      *outputs, "      break;", "    }"])
    return "\n".join([*lines, "  }", "}"])


def expected(label, widths, values):
    params = [bitvec.param(f"a{k}", width) for k, width in enumerate(widths)]
    expression = bitvec.lowered(bitvec.DEFINED[label].expand(*params))
    return evaluate(expression, None, dict(zip((p.args[0] for p in params), values)))


@pytest.mark.native
@pytest.mark.parametrize("backend", ("softfloat", "host"))
def test_generated_functions(real_uses, backend):
    uses = sorted(set(real_uses + extra_uses()))
    # Batch compilation bounds translation-unit size and reuses build.library's source cache.
    for start in range(0, len(uses), 48):
        batch = uses[start:start + 48]
        prefix = "#undef SASS_FP_SOFT\n" + ("#define SASS_FP_SOFT\n" if backend == "softfloat" else "")
        source = prefix + definitions.definitions_header(batch).replace("#pragma once\n", "") + wrapper(batch)
        lib = build.library(source, f"definitions-{backend}")
        fn = lib.run
        fn.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]
        fn.restype = None
        for i, (label, widths) in enumerate(batch):
            for values in vectors(widths):
                limbs = [limb for value in values for limb in (value & bitvec.mask(64), value >> 64)]
                inputs = (ctypes.c_uint64 * max(1, len(limbs)))(*limbs)
                output = (ctypes.c_uint64 * 2)()
                fn(i, inputs, output)
                assert output[0] | output[1] << 64 == expected(label, widths, values), (backend, label, values)


@pytest.mark.gpu
def test_generated_functions_cuda(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    uses = set(extra_uses()[:1])
    for text in ("FMNMX.FTZ R0, R2, R3, PT", "FSETP.LTU.AND P0, PT, R2, R3, PT",
                 "F2F.F16.F32 R0, R2", "PRMT R0, R2, R4, R3", "IMAD.WIDE.U32 R0, R2, R3, R4"):
        uses.update(uses_of(arch, text))
    uses = sorted(uses)
    source = definitions.definitions_header(uses).replace("#pragma once\n", "") + wrapper(uses, device=True)
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
    data, want = [], []
    for i, (label, widths) in enumerate(uses):
        for values in vectors(widths):
            limbs = [limb for value in values for limb in (value & bitvec.mask(64), value >> 64)]
            data.extend([*limbs, *([0] * (6 - len(limbs))), i, 0])
            value = expected(label, widths, values)
            want.extend((value & bitvec.mask(64), value >> 64))
    rows = len(want) // 2
    padding = (-rows) % 32
    data.extend(data[:8] * padding); want.extend(want[:2] * padding)
    # Two threads compute one row, each returning one u64 limb through the existing driver.
    fixture = SimpleNamespace(image=image.read_bytes(), kernel="run",
                              data=struct.pack("<" + "Q" * len(data), *data), threads=2 * (rows + padding),
                              block=32, output_size=8, arguments=())
    driver = gpu.Driver(arch)
    try: got = driver.run(fixture)
    finally: driver.close()
    assert got == want
