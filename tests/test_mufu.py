"""The MUFU runtime agrees with independent references and captured SM89 words."""
import gzip
from pathlib import Path
import random
import struct

import pytest

from sass.semantics import runtime
from sass.semantics.fp import mufu
import tests.refs.mufu


@pytest.mark.parametrize('op', ['SIN', 'COS'])
def test_reference_matches_recorded_gpu(op):
    inputs, outputs = tests.refs.mufu.samples()
    assert [tests.refs.mufu.sincos(bits, op) for bits in inputs] == outputs[op]


@pytest.mark.parametrize('op', ['SIN', 'COS'])
def test_runtime_matches_recorded_gpu(op):
    cpu = runtime.Runtime(89)
    inputs, outputs = tests.refs.mufu.samples()
    assert [cpu.call(f'sass_prim_mufu_{op.lower()}', [bits], 4) for bits in inputs] == outputs[op]


@pytest.mark.parametrize('name, args', [('mufu_rcp', [0x3e800000]), ('mufu_sin', [0x3c00, 0]), ('mufu_cos', [0x3c00, 1])])
def test_unresolved_mufu_still_fails(name, args):
    with pytest.raises(runtime.UnsupportedSemantics, match='MUFU approximation'):
        runtime.Runtime(89).call('sass_prim_' + name, args, 4)


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
