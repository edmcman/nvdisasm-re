"""The MUFU runtime agrees with independent references and captured SM89 words."""
import gzip
import hashlib
import json
from pathlib import Path
import random
import struct

import pytest

from sass.emulate import native
from sass.semantics import runtime
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


@pytest.mark.parametrize('name, args', [('mufu_ex2', [0x3e800000]), ('mufu_sin', [0x3c00, 0]), ('mufu_rcp', [0x3c00, 1])])
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
            assert native.sincos(0x3e800000, 'SIN') == hardware
        elif exhaustive or offset in selected:
            assert native.reduced_sine(offset) == hardware, hex(offset)


def test_coefficient_copy_matches_hardware_recovery():
    assert native.mufu_coefficients() == tests.refs.mufu.coefficients()


@pytest.mark.parametrize('op', ['RCP', 'RSQ', 'RCP64H', 'RSQ64H'])
def test_reciprocal_runtime_matches_recorded_bounds(op):
    inputs, outputs = tests.refs.mufu.samples()
    fmt = '64H' if op.endswith('64H') else 'F32'
    cpu = runtime.Runtime(89)
    for bits, hardware in zip(inputs, outputs[op]):
        expected = tests.refs.mufu.reciprocal(bits, op, fmt)
        got = cpu.call('sass_prim_mufu_' + op.lower(), [bits], 4)
        assert got == expected, (op, hex(bits), hex(got), hex(expected))
        assert hardware == expected, (op, hex(bits), hex(hardware), hex(expected))
        assert tests.refs.mufu.reciprocal_same(hardware, tests.refs.mufu.reciprocal_ideal(bits, op, fmt), op, fmt)


@pytest.mark.parametrize('op', ['RCP', 'RSQ'])
def test_fp16_reciprocal_runtime_matches_complete_capture(op):
    cpu = runtime.Runtime(89)
    hardware = tests.refs.mufu.reciprocal_halves(op)
    assert all(cpu.call('sass_prim_mufu_' + op.lower(), [bits, 0], 2) == word
               for bits, word in enumerate(hardware))


@pytest.mark.parametrize('op', ['RCP', 'RSQ'])
def test_fp16_extract_and_unselected_half_independence(op):
    path = Path(__file__).parents[1] / f'research/mufu/reciprocal/SM89-{op.lower()}-H1.json.gz'
    capture = json.loads(gzip.decompress(path.read_bytes()))
    assert capture['inputs'] == list(range(65536))
    assert capture['outputs'] == tests.refs.mufu.reciprocal_halves(op)


@pytest.mark.parametrize('fmt', ['F32', '64H'])
@pytest.mark.parametrize('op', ['RCP', 'RSQ'])
@pytest.mark.parametrize('suffix', ['', '-offsets', '-holdout', '-holdout2', '-holdout3', '-refined'])
def test_normalized_domains_and_exponent_boundaries(op, fmt, suffix, request):
    path = Path(__file__).parents[1] / f'research/mufu/reciprocal/SM89-{op.lower()}-{fmt}{suffix}.json.gz'
    capture = json.loads(gzip.decompress(path.read_bytes()))
    operation = op + ('64H' if fmt == '64H' else '')
    assert capture['form']['mufuop'] == operation
    cpu = runtime.Runtime(89)
    mantissa, bias = (23, 127) if fmt == 'F32' else (20, 1023)
    selected = set(range(len(capture['inputs']))) if request.config.getoption('--exhaustive') else set(
        random.Random(117).sample(range(len(capture['inputs'])), min(8192, len(capture['inputs']))))
    selected.update(i for i, bits in enumerate(capture['inputs']) if bits >> mantissa not in (bias, bias + 1))
    for i in sorted(selected):
        bits, hardware = capture['inputs'][i], capture['outputs'][i]
        expected = tests.refs.mufu.reciprocal(bits, operation, fmt)
        got = cpu.call('sass_prim_mufu_' + operation.lower(), [bits], 4)
        assert got == expected, (operation, hex(bits), hex(got), hex(expected))
        assert hardware == got, (
            operation, hex(bits), hex(got), hex(hardware))


@pytest.mark.parametrize('fmt', ['F32', '64H'])
@pytest.mark.parametrize('op', ['RCP', 'RSQ'])
def test_complete_reciprocal_capture(op, fmt, request):
    path = Path(__file__).parents[1] / f'research/mufu/reciprocal/SM89-{op.lower()}-{fmt}-full.json'
    capture = json.loads(path.read_text())
    payload = (path.parent / capture['deltas']).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == capture['sha256']
    raw = gzip.decompress(payload)
    count = 1 << capture['mantissa_bits']
    assert capture['parities'] == ([0] if op == 'RCP' else [0, 1])
    assert capture['count'] == count * len(capture['parities'])
    assert len(raw) == capture['count'] * 4
    selected = set(random.Random(117).sample(range(capture['count']), 8192))
    width = capture['mantissa_bits'] - (7 if op == 'RCP' else 6)
    selected.update(parity * count + (segment << width) + local
                    for parity in capture['parities'] for segment in range(count >> width)
                    for local in (0, 1, (1 << width) // 2, (1 << width) - 1))
    operation = op + ('64H' if fmt == '64H' else '')
    cpu, hardware = runtime.Runtime(89), 0
    exhaustive = request.config.getoption('--exhaustive')
    for i, (delta,) in enumerate(struct.iter_unpack('<i', raw)):
        hardware += delta
        if exhaustive or i in selected:
            parity, offset = divmod(i, count)
            bits = ((capture['exponent_bias'] + parity) << capture['mantissa_bits']) + offset
            got = cpu.call('sass_prim_mufu_' + operation.lower(), [bits], 4)
            assert got == hardware, (operation, hex(bits), hex(got), hex(hardware))
