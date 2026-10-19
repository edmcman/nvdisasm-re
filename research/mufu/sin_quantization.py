"""Recover and exhaustively check a fixed-point model of the SM89 SIN evaluator.

PYTHONPATH=. python3 research/mufu/sin_quantization.py

Reads the existing GPU captures. The arithmetic rule was discovered from segment
0; coefficients are fitted on 8,192 random offsets per segment, snapped to integer
grids, then checked against every offset. This is retrospective discovery with
disjoint coefficient-fitting/holdout indices, not a new blinded GPU experiment.
"""
import hashlib
import json
from pathlib import Path

import numpy

from research.mufu import sin_fit, sincos_reduction

OUT = sin_fit.DEFAULT
Q = sin_fit.Q
ACCUMULATOR_BITS = 37


def square_products(offsets, width_bits=17):
    """Retain square terms at/above bit 2*width_bits-15, before adding them.

    For width_bits=17 this drops every diagonal term 2^(2*i) below 2^19
    and every combined symmetric cross term 2^(i+j+1) below 2^19.
    High seven input bits contribute all high/high terms; the remaining loop
    adds only retained high/low partial products, with no discarded-bit carries.
    """
    low_bits = width_bits - 7
    high = offsets >> low_bits
    low = offsets & ((1 << low_bits) - 1)
    result = 2 * high * high
    for bit in range(7):
        shift = low_bits - 2 - bit
        part = low >> shift if shift >= 0 else low << -shift
        result += ((high >> bit) & 1) * part
    return result


def fixed_to_float32(accumulator):
    """Positive integer * 2^-37 to binary32, discarding excess significand bits.

    Integers in this model fit in 38 bits, so frexp's float64 conversion is exact
    and is used only to find their leading bit. All significand rounding is
    integer shifting. Zero is +0; all nonzero outputs in this domain are normal.
    """
    if numpy.any(accumulator < 0):
        raise ValueError('negative reduced sine accumulator')
    leading = numpy.frexp(numpy.maximum(accumulator, 1).astype(numpy.float64))[1].astype(numpy.int64) - 1
    right = numpy.maximum(leading - 23, 0)
    left = numpy.maximum(23 - leading, 0)
    significand = (accumulator >> right) << left
    exponent = leading - ACCUMULATOR_BITS + 127
    words = ((exponent << 23) | (significand & 0x7fffff)).astype(numpy.uint32)
    return numpy.where(accumulator == 0, 0, words).astype(numpy.uint32)


def evaluate(model, reduced_offsets, square='partial_products', rounding='toward_zero'):
    """Evaluate a reduced offset in [0,2^23); return positive float32 words."""
    if numpy.any((reduced_offsets < 0) | (reduced_offsets >= Q)):
        raise ValueError('reduced offset outside first quadrant')
    width_bits = model['width_bits']
    segment = reduced_offsets >> width_bits
    local = reduced_offsets & ((1 << width_bits) - 1)
    coefficients = numpy.array(model['coefficients'], dtype=numpy.int64)[segment]
    if square == 'partial_products':
        squared = square_products(local, width_bits)
    elif square == 'square_then_truncate':
        squared = (local * local) >> (2 * width_bits - 15)
    else:
        raise ValueError(square)
    accumulator = (coefficients[:, 0] << 12) + coefficients[:, 1] * local + coefficients[:, 2] * squared
    if rounding == 'toward_zero':
        return fixed_to_float32(accumulator)
    if rounding == 'nearest_even':
        return (accumulator / 2.0 ** ACCUMULATOR_BITS).astype(numpy.float32).view(numpy.uint32)
    raise ValueError(rounding)


def infer(outputs, segments):
    width_bits = 23 - (segments.bit_length() - 1)
    width = 1 << width_bits
    local = numpy.arange(width, dtype=numpy.int64)
    t = local / width
    matrix = numpy.stack((numpy.ones(width), t, square_products(local, width_bits) / 32768), axis=1)
    training = numpy.random.default_rng(22).choice(width, 8192, replace=False)
    coefficients = []
    scales = numpy.array([2 ** 25, 2 ** (ACCUMULATOR_BITS - width_bits), 2 ** 22])
    for segment in range(segments):
        words = outputs[segment * width:(segment + 1) * width]
        y = sin_fit.values(words)
        # A toward-zero output represents [y,next_float(y)). Fit its midpoint,
        # then snap coefficients to the observed fixed-point grids.
        ulp = numpy.spacing(words.view(numpy.float32)).astype(numpy.float64)
        target = y + ulp / 2
        if segment == 0:
            fit = numpy.concatenate(([0.0], numpy.linalg.lstsq(
                matrix[training, 1:], target[training], rcond=None)[0]))
        else:
            fit = numpy.linalg.lstsq(matrix[training], target[training], rcond=None)[0]
        coefficients.append(numpy.rint(fit * scales).astype(numpy.int64).tolist())
    return dict(kind='truncated_square_fixed_point', segments=segments, width_bits=width_bits,
        accumulator_fraction_bits=ACCUMULATOR_BITS, constant_fraction_bits=25,
        linear_fraction_bits=ACCUMULATOR_BITS - width_bits, quadratic_fraction_bits=22,
        square_output_fraction_bits=15, final_rounding='toward_zero',
        coefficients=coefficients), training


def statistics(got, want):
    distance = numpy.abs(got.astype(numpy.int64) - want.astype(numpy.int64))
    return dict(n=len(want), exact=int(numpy.count_nonzero(distance == 0)),
        mismatches=int(numpy.count_nonzero(distance)), exact_fraction=float(numpy.mean(distance == 0)),
        max_ulp=int(distance.max()) if len(distance) else 0)


def compare_segments(model, outputs, training):
    width = 1 << model['width_bits']
    heldout = numpy.ones(width, dtype=bool)
    heldout[training] = False
    rows = []
    for segment in range(model['segments']):
        r = numpy.arange(segment * width, (segment + 1) * width, dtype=numpy.int64)
        got = evaluate(model, r)
        want = outputs[r]
        rows.append(dict(segment=segment, all=statistics(got, want),
            training=statistics(got[training], want[training]),
            holdout=statistics(got[heldout], want[heldout])))
    total = dict(n=sum(s['all']['n'] for s in rows), exact=sum(s['all']['exact'] for s in rows),
        mismatches=sum(s['all']['mismatches'] for s in rows), max_ulp=max(s['all']['max_ulp'] for s in rows))
    total['exact_fraction'] = total['exact'] / total['n']
    return dict(total=total, exact_segments=[s['segment'] for s in rows if not s['all']['mismatches']],
        training_offsets=len(training) * model['segments'],
        holdout_offsets=(width - len(training)) * model['segments'], segments=rows)


def input_words(model, ops, inputs):
    """Combine the earlier reduction hypothesis with this evaluator, including NaNs."""
    result = numpy.full(len(inputs), 0x7fffffff, dtype=numpy.uint32)
    finite = [i for i, word in enumerate(inputs) if word & 0x7f800000 != 0x7f800000]
    coordinates = [sincos_reduction.reduced(ops[i], inputs[i]) for i in finite]
    offsets = numpy.array([r for r, _ in coordinates], dtype=numpy.int64)
    signs = numpy.array([sign for _, sign in coordinates], dtype=numpy.uint32)
    result[finite] = evaluate(model, offsets) ^ signs
    return result


def saved_samples(model):
    raw = sincos_reduction.load(sin_fit.REDUCTION / 'observations.json.gz')['observations']
    actual = input_words(model, [v['op'] for v in raw], [int(v['input'], 16) for v in raw])
    shared = statistics(actual, numpy.array([v['registers']['R8'] for v in raw], dtype=numpy.uint32))
    path = Path(__file__).parents[2] / 'tests/refs/mufu_samples.json'
    data = json.loads(path.read_text())
    words = lambda text: [int(text[i:i + 8], 16) for i in range(0, len(text), 8)]
    inputs = words(data['inputs'])
    samples = {}
    for op in ('SIN', 'COS'):
        samples[op] = statistics(input_words(model, [op] * len(inputs), inputs),
                                 numpy.array(words(data['outputs'][op]), dtype=numpy.uint32))
    return dict(shared_harness=shared, original_samples=samples)


def implementation_checks():
    # Independent scalar enumeration of every square term validates the fast
    # high/low formula. Integer bit_length validates the conversion separately.
    rng = numpy.random.default_rng(0x535155)
    for width_bits in (16, 17, 18):
        offsets = numpy.unique(numpy.concatenate((numpy.arange(128),
            rng.integers(0, 1 << width_bits, 2048), numpy.array([(1 << width_bits) - 1]))))
        cutoff = 2 * width_bits - 15
        expected = []
        for value in offsets:
            value = int(value)
            total = 0
            for i in range(width_bits):
                if not (value >> i) & 1:
                    continue
                if 2 * i >= cutoff:
                    total += 1 << (2 * i - cutoff)
                for j in range(i):
                    if (value >> j) & 1 and i + j + 1 >= cutoff:
                        total += 1 << (i + j + 1 - cutoff)
            expected.append(total)
        if not numpy.array_equal(square_products(offsets, width_bits), expected):
            raise AssertionError('partial-product enumeration mismatch')
    accumulators = numpy.unique(numpy.concatenate((numpy.array([0, 1]),
        rng.integers(1, 1 << 38, 4096), numpy.array([1 << i for i in range(38)]))))
    expected = []
    for integer in accumulators:
        integer = int(integer)
        if integer == 0:
            expected.append(0)
            continue
        leading = integer.bit_length() - 1
        mantissa = integer >> (leading - 23) if leading >= 23 else integer << (23 - leading)
        expected.append(((leading - 37 + 127) << 23) | (mantissa & 0x7fffff))
    if not numpy.array_equal(fixed_to_float32(accumulators), expected):
        raise AssertionError('integer float32 conversion mismatch')


def main():
    implementation_checks()
    outputs, metadata = sin_fit.read_table(OUT)
    comparisons = {}
    selected = None
    for segments in (32, 64, 128):
        model, training = infer(outputs, segments)
        result = compare_segments(model, outputs, training)
        comparisons[str(segments)] = result
        print(f'{segments} segments: {result["total"]}', flush=True)
        if segments == 64:
            selected = model
    if comparisons['64']['total']['mismatches']:
        raise AssertionError('64-segment model failed exhaustive comparison')
    (OUT / 'quantized-model.json').write_text(json.dumps(selected, indent=2) + '\n')
    ablations = {}
    for square, rounding in (('partial_products', 'nearest_even'), ('square_then_truncate', 'toward_zero')):
        totals = dict(n=0, exact=0, mismatches=0, max_ulp=0)
        for start in range(0, Q, 1 << 18):
            r = numpy.arange(start, start + (1 << 18), dtype=numpy.int64)
            row = statistics(evaluate(selected, r, square, rounding), outputs[r])
            for key in ('n', 'exact', 'mismatches'): totals[key] += row[key]
            totals['max_ulp'] = max(totals['max_ulp'], row['max_ulp'])
        totals['exact_fraction'] = totals['exact'] / totals['n']
        ablations[square + '_' + rounding] = totals
    endpoint = statistics(evaluate(selected, numpy.array([Q - 1])), outputs[Q:Q + 1])
    report = dict(output_sha256=metadata['output_sha256'], numpy_version=numpy.__version__,
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), coefficient_seed=22,
        comparisons=comparisons, quarter_turn_endpoint=endpoint, saved_inputs=saved_samples(selected),
        ablations=ablations, implementation_checks='independent scalar square-term enumeration and integer float conversion passed',
        interpretation='Arithmetic discovered retrospectively using captured segment-0 outputs. Coefficients '
        'are inferred from 8192 offsets per segment and snapped to integer grids; remaining offsets are '
        'disjoint coefficient holdouts. Entire reduced domain verified against captured SM89 data; '
        'argument reduction over all binary32 inputs and other architectures are not exhaustively verified.')
    (OUT / 'quantization.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(saved_inputs=report['saved_inputs'], ablations=ablations), indent=2))


if __name__ == '__main__':
    main()
