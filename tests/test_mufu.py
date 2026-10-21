"""The MUFU runtime agrees with independent references and captured SM89 words."""
import gzip
from pathlib import Path
import random
import struct

import pytest

from sass.decode import ir
from sass.semantics import runtime
from sass.semantics.fp import mufu
from tests.harness.select import arches
import tests.refs.mufu


def pytest_generate_tests(metafunc):
    if 'primitive_arch' in metafunc.fixturenames:
        metafunc.parametrize('primitive_arch', arches(metafunc.config))


@pytest.mark.parametrize('op', ['SIN', 'COS'])
def test_reference_matches_recorded_gpu(op):
    inputs, outputs = tests.refs.mufu.samples()
    assert [tests.refs.mufu.sincos(bits, op) for bits in inputs] == outputs[op]


@pytest.mark.parametrize('op', ['SIN', 'COS'])
def test_runtime_matches_recorded_gpu(primitive_arch, op):
    arch = ir.load(primitive_arch)
    k = next(k for k in arch.classes if k.name == 'mufu__RRR_RR')
    cpu = runtime.Runtime('.', int(primitive_arch[2:]))
    field = 'mufuop' if 'mufuop' in k.operand_types else 'op'
    selector = arch.enums[k.operand_types[field].type][op]
    inputs, outputs = tests.refs.mufu.samples()
    assert [cpu.execute(arch, k, dict(Rb=bits, **{field: selector}), 'Rd', 4)
            for bits in inputs] == outputs[op]


def test_runtime_sources_and_modifiers(primitive_arch):
    arch = ir.load(primitive_arch)
    cpu = runtime.Runtime('.', int(primitive_arch[2:]))
    for k in arch.classes:
        if not k.name.startswith('mufu__') or k.alternate:
            continue
        source = next(n for n in ('Rb', 'Sb', 'URb') if n in k.operand_types)
        for op in ('SIN', 'COS'):
            field = 'mufuop' if 'mufuop' in k.operand_types else 'op'
            selector = arch.enums[k.operand_types[field].type][op]
            for bits in (0x80000000, 0x80000001, 0xbdcccccd, 0x3daaaaab, 0xff800000, 0xff800001):
                for absolute in (False, True):
                    for negate in (False, True):
                        # A CX descriptor must not be mistaken for the loaded Sb value.
                        values = dict(URb=0x12345678, **{field: selector})
                        values.update({source: bits, source+'@absolute': absolute, source+'@negate': negate})
                        assert cpu.execute(arch, k, values, 'Rd', 4) == tests.refs.mufu.sincos(bits, op, absolute, negate)


def test_unresolved_mufu_still_fails(primitive_arch):
    arch = ir.load(primitive_arch)
    cpu = runtime.Runtime('.', int(primitive_arch[2:]))
    for name, op in (('mufu__RRR_RR', 'RCP'), ('mufu_fp16__RR', 'SIN'), ('mufu_fp16__RR', 'COS')):
        k = next(k for k in arch.classes if k.name == name)
        field = 'mufuop' if 'mufuop' in k.operand_types else 'op'
        values = dict(Rb=0x3e800000, **{field: arch.enums[k.operand_types[field].type][op]})
        with pytest.raises(runtime.UnsupportedSemantics, match='MUFU approximation'):
            cpu.execute(arch, k, values, 'Rd', 4)


def test_reduced_evaluator_matches_captured_gpu(request):
    """Sample all segments by default; --exhaustive checks every captured grid offset."""
    path = Path(__file__).parents[1] / 'research/mufu/sin-fit/sin-deltas.i32.gz'
    raw = gzip.decompress(path.read_bytes())
    assert len(raw) == ((1 << 23) + 1) * 4
    selected = set(random.Random(0x4d554655).sample(range(1 << 23), 8192))
    selected.update((segment << 17) + local for segment in range(64)
                    for local in (0, 1, 1023, 1024, 1280, 65535, 65536, 131071))
    exhaustive = request.config.getoption('--exhaustive') or request.config.getoption('--sample') == 100
    hardware = 0
    for offset, (delta,) in enumerate(struct.iter_unpack('<i', raw)):
        hardware += delta
        if offset == 1 << 23:
            assert mufu.sincos(0x3e800000, 'SIN') == hardware
        elif exhaustive or offset in selected:
            assert mufu.reduced_sine(offset) == hardware, hex(offset)


def test_coefficient_copy_matches_hardware_recovery():
    assert list(map(list, mufu.COEFFICIENTS)) == tests.refs.mufu.coefficients()
