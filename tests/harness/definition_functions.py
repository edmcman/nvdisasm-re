"""Bounded discovery and compilation shared by the definition backend tests."""
import ctypes
import itertools
import os
import time
from functools import cache

from sass import workers
from sass.emulate import build, definitions
from sass.semantics import bitvec, lifter
from sass.sleigh.build import ARCHES
from tests import cases
from tests.harness import sampling, sharded

VARIANTS = tuple(itertools.product(ARCHES, ("exact", "readable")))
BATCH = 48


def roots(semantics):
    out = [semantics.guard, *(value for _, value in semantics.writes)]
    for store in semantics.stores: out.extend((store.cond, store.address, store.value))
    for effect in semantics.effects: out.extend(effect.args)
    if semantics.flow: out.extend((semantics.flow.cond, semantics.flow.target))
    return out


def representatives(job):
    """One representative word per use in a shard, never returning expression graphs."""
    arch, fp, fam, sample, shard = job
    words, found = set(), {}
    for case in cases.stream(arch, fp, fam, sample, shard):
        word = case.request.get("word")
        if word is None or word in words: continue
        words.add(word)
        sem = lifter.lift(arch, bytes.fromhex(word), fp=fp, conditions=False)
        exprs = roots(sem)
        definitions.definition_uses(exprs)  # validate the argument grammar
        for node in definitions.nodes(exprs):
            if node.op not in bitvec.DEFINED: continue
            use = node.op, tuple(arg.w for arg in node.args)
            previous = found.setdefault(use, (word, node.w))
            assert previous[1] == node.w, (arch, fp, use, previous, node.w)
    return arch, fp, shard is not None, found


@cache
def discover():
    """Discover once per session at the default sample, independent of CLI sampling."""
    begin = time.monotonic()
    uses, origins, lifted, result_widths = set(), set(), set(), {}
    current_arch = None
    for arch, fp, generated, found in sharded.run(
        representatives, VARIANTS, cases.FAMILIES, sampling.DEFAULT,
        workers=workers.fitting(2, os.cpu_count() or 1),
    ):
        if arch != current_arch:
            print(f"\ndefinition discovery: {arch}", flush=True)
            current_arch = arch
        origins.add((arch, fp, generated))
        for use, (word, result_width) in found.items():
            assert result_widths.setdefault(use, result_width) == result_width, (arch, fp, use)
            if use not in uses:
                key = arch, fp, word
                if key not in lifted:
                    sem = lifter.lift(arch, bytes.fromhex(word), fp=fp, conditions=False)
                    # Register expansions in the parent, even when workers used forkserver.
                    definitions.definition_uses(roots(sem))
                    lifted.add(key)
                label, widths = use
                params = [bitvec.param(f"a{k}", width) for k, width in enumerate(widths)]
                assert bitvec.DEFINED[label].expand(*params).w == result_width, (arch, fp, use)
                uses.add(use)
    assert origins == {(arch, fp, generated) for arch, fp in VARIANTS for generated in (False, True)}
    print(f"\ndefinition discovery: {len(uses)} signatures in {time.monotonic() - begin:.2f}s", flush=True)
    return sorted(uses)


def wrapper(uses, device=False):
    """Dispatch with arbitrary arity; every argument/result uses two u64 limbs."""
    stride = 1 + 2 * max((len(widths) for _, widths in uses), default=0)
    head = (
        'extern "C" __global__ void run(const sass::u64 *input, sass::u64 *output) {\n'
        '  const unsigned thread = blockIdx.x * blockDim.x + threadIdx.x;\n'
        f'  const sass::u64 *row = input + (thread / 2) * {stride};\n'
        '  const sass::u64 *a = row + 1;\n'
        '  sass::u64 *out = output + thread;\n'
        '  const unsigned which = row[0];'
    ) if device else 'extern "C" void run(unsigned which, const sass::u64 *a, sass::u64 *out) {'
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


def compiled(uses, backend):
    prefix = "" if backend == "softfloat" else "#undef SASS_FP_SOFT\n"
    source = prefix + definitions.definitions_header(uses).replace("#pragma once\n", "") + wrapper(uses)
    fn = build.library(source, f"definitions-{backend}").run
    fn.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]
    fn.restype = None
    return fn


def call(fn, which, values):
    limbs = [limb for value in values for limb in (value & bitvec.mask(64), value >> 64)]
    inputs = (ctypes.c_uint64 * max(1, len(limbs)))(*limbs)
    output = (ctypes.c_uint64 * 2)()
    fn(which, inputs, output)
    return output[0] | output[1] << 64
