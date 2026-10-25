"""Fit the SM89 reduced-angle SIN evaluator, separately from argument reduction.

GPU capture (outside the sandbox):
  PYTHONPATH=src:. uv run --with numpy python research/mufu/sin_fit.py capture
CPU fitting and replay:
  PYTHONPATH=. python3 research/mufu/sin_fit.py fit

The training offsets come from the earlier reduction experiment. Model selection
uses a disjoint deterministic sample; the selected piecewise quadratic is also
checked against every remaining reduced offset. This is a fitted CPU model, not
a reconstruction of NVIDIA's coefficient table or internal arithmetic.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

import numpy

from research.mufu import sincos_reduction

Q = 1 << 23
TURN = 1 << 25
DEFAULT = Path(__file__).with_name('sin-fit')
REDUCTION = Path(__file__).with_name('sincos-reduction')


def training_data():
    """Only the earlier experiment supplies fitting data, never the new table."""
    raw = sincos_reduction.load(REDUCTION / 'observations.json.gz')
    values = {}
    for row in raw['observations']:
        bits = int(row['input'], 16)
        if bits & 0x7f800000 == 0x7f800000:
            continue
        offset, sign = sincos_reduction.reduced(row['op'], bits)
        output = row['registers']['R8'] ^ sign
        if offset in values and values[offset] != output:
            raise ValueError('inconsistent earlier reduction observations')
        values[offset] = output
    offsets = numpy.array(sorted(values), dtype=numpy.int64)
    words = numpy.array([values[int(r)] for r in offsets], dtype=numpy.uint32)
    return offsets, words, raw['metadata']


def capture(directory):
    from research.mufu import probe
    from sass import paths
    from tests.harness import templates
    from tests.harness.gpu import Driver, Template

    directory.mkdir(parents=True, exist_ok=True)
    fixture = probe.mufu_fixture('SIN', [0] * 32)
    target = Template(fixture.image, fixture.kernel).decoded(templates.SLOTS[0])
    if target.klass.name != 'mufu__RRR_RR' or target.env['Rd'] != 11 or target.env['Rb'] != 2:
        raise ValueError('unexpected bulk capture target')
    (directory / 'sin-kernel.cubin').write_bytes(fixture.image)
    chunk = 1 << 18
    outputs = numpy.empty(Q + 1, dtype=numpy.uint32)
    driver = Driver()
    try:
        device = driver.device
        runner = probe.Runner(driver, 'SIN', chunk)
        try:
            for start in range(0, Q + 1, chunk):
                count = min(chunk, Q + 1 - start)
                padded = (count + 255) & -256
                offsets = numpy.arange(start, start + padded, dtype=numpy.uint32)
                inputs = (offsets.astype(numpy.float32) / numpy.float32(TURN)).view(numpy.uint32)
                outputs[start:start + count] = runner(inputs)[:count]
                print(f'captured {start + count}/{Q + 1} reduced offsets', flush=True)
        finally:
            runner.close()
    finally:
        driver.close()
    # The earlier dataset uses independent generated native wrappers. Compare
    # every one of its distinct offsets before accepting the patched bulk runner.
    offsets, words, earlier = training_data()
    mismatches = int(numpy.count_nonzero(outputs[offsets] != words))
    differences = numpy.diff(outputs.astype(numpy.int64), prepend=0).astype('<i4')
    data = differences.tobytes()
    (directory / 'sin-deltas.i32.gz').write_bytes(gzip.compress(data, mtime=0))
    metadata = dict(arch='SM89', device=device, captured_utc=datetime.now(timezone.utc).isoformat(),
        base_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        kernel_sha256=hashlib.sha256(fixture.image).hexdigest(),
        output_sha256=hashlib.sha256(outputs.astype('<u4').tobytes()).hexdigest(),
        compiler=subprocess.check_output([paths.NVCC, '--version'], text=True).strip(),
        target_instruction=target.text, target_offset=templates.SLOTS[0],
        input_definition='binary32(r / 2^25), every integer r from 0 through 2^23 inclusive',
        output_format='gzip of little-endian signed int32 differences of output uint32 words; cumulative sum recovers words',
        outputs=Q + 1, calibration_offsets=len(offsets), calibration_mismatches=mismatches,
        calibration_device=earlier['device'],
        instruction_origin='raw MUFU.SIN patched into pinned carry_semantics kernel, variable-latency scoreboard scheduling')
    (directory / 'capture.json').write_text(json.dumps(metadata, indent=2) + '\n')
    if mismatches:
        raise RuntimeError(f'bulk/shared-wrapper disagreement: {mismatches} offsets')
    print(f'Bulk runner agrees with all {len(offsets)} earlier shared-wrapper offsets.', flush=True)


def read_table(directory):
    metadata = json.loads((directory / 'capture.json').read_text())
    data = gzip.decompress((directory / 'sin-deltas.i32.gz').read_bytes())
    outputs = numpy.cumsum(numpy.frombuffer(data, dtype='<i4'), dtype=numpy.int64).astype(numpy.uint32)
    if len(outputs) != Q + 1:
        raise ValueError('incomplete evaluator table')
    if hashlib.sha256(outputs.astype('<u4').tobytes()).hexdigest() != metadata['output_sha256']:
        raise ValueError('evaluator table checksum mismatch')
    if metadata['calibration_mismatches']:
        raise ValueError('bulk/shared-wrapper calibration failed')
    return outputs, metadata


def values(words):
    return words.view(numpy.float32).astype(numpy.float64)


def polynomial_fit(offsets, y, degree):
    u = offsets / Q
    # Force the known zero exactly and use odd powers for an ordinary sine fit.
    powers = list(range(1, degree + 1, 2))
    matrix = numpy.stack([u ** power for power in powers], axis=1)
    coefficients = numpy.linalg.lstsq(matrix, y, rcond=None)[0]
    return dict(kind='global_odd', degree=degree, coefficients=coefficients.tolist())


def piecewise_fit(offsets, y, segments, degree):
    segment = numpy.minimum(offsets * segments // Q, segments - 1)
    coefficients = []
    for index in range(segments):
        mask = segment == index
        t = offsets[mask] / (Q / segments) - index
        # Preserve SIN(+0) exactly; infer the first segment's linear gain.
        first_power = 1 if index == 0 else 0
        matrix = numpy.stack([t ** p for p in range(first_power, degree + 1)], axis=1)
        fit = numpy.linalg.lstsq(matrix, y[mask], rcond=None)[0].tolist()
        coefficients.append(([0.0] if index == 0 else []) + fit)
    return dict(kind='piecewise', segments=segments, degree=degree, coefficients=coefficients)


def evaluate(model, offsets):
    u = offsets / Q
    if model['kind'] == 'ideal_sine':
        return numpy.sin((numpy.pi / 2) * u)
    if model['kind'] == 'global_odd':
        result = numpy.zeros_like(u)
        for index, coefficient in enumerate(model['coefficients']):
            result += coefficient * u ** (2 * index + 1)
        return result
    segment = numpy.minimum(offsets * model['segments'] // Q, model['segments'] - 1)
    t = u * model['segments'] - segment
    coefficients = numpy.array(model['coefficients'])[segment]
    result = numpy.zeros_like(u)
    for power in range(model['degree'], -1, -1):
        result = result * t + coefficients[:, power]
    return result


def statistics(model, offsets, words):
    predicted = evaluate(model, offsets).astype(numpy.float32)
    predicted_words = predicted.view(numpy.uint32)
    # Signed numeric ordering also makes the metric valid if a bad fit goes negative.
    def ordered(bits):
        magnitude = (bits & 0x7fffffff).astype(numpy.int64)
        return numpy.where(bits >> 31, -magnitude, magnitude)
    distance = numpy.abs(ordered(predicted_words) - ordered(words))
    error = numpy.abs(predicted.astype(numpy.float64) - values(words))
    worst = int(numpy.argmax(error))
    return dict(n=len(offsets), exact=int(numpy.count_nonzero(predicted_words == words)),
        exact_fraction=float(numpy.mean(predicted_words == words)),
        max_ulp=int(distance.max()), within_1_ulp_fraction=float(numpy.mean(distance <= 1)),
        max_absolute_error=float(error[worst]), rms_absolute_error=float(numpy.sqrt(numpy.mean(error ** 2))),
        worst_offset=int(offsets[worst]), got=f'{int(predicted_words[worst]):08x}',
        hardware=f'{int(words[worst]):08x}')


def fit(directory):
    outputs, metadata = read_table(directory)
    offsets, training_words, _ = training_data()
    if not numpy.array_equal(outputs[offsets], training_words):
        raise ValueError('training observations differ from captured evaluator table')
    training = numpy.zeros(Q + 1, dtype=bool)
    training[offsets] = True
    rng = numpy.random.default_rng(0x53494e)
    candidates = numpy.unique(numpy.concatenate((
        rng.integers(0, Q + 1, size=80000), numpy.arange(4096),
        numpy.array([b + delta for b in range(0, Q + 1, Q // 64)
                     for delta in (-2, -1, 0, 1, 2) if 0 <= b + delta <= Q]))))
    validation = candidates[~training[candidates]]
    y = values(training_words)
    models = [('ideal_sine', dict(kind='ideal_sine'))]
    models += [(f'odd_degree_{degree}', polynomial_fit(offsets, y, degree))
               for degree in (3, 5, 7, 9, 11, 15)]
    models += [(f'segments_{segments}_degree_{degree}', piecewise_fit(offsets, y, segments, degree))
               for segments in (16, 32, 64, 128, 256, 512) for degree in (2, 3)]
    results = {}
    for name, model in models:
        results[name] = dict(training=statistics(model, offsets, training_words),
                             validation=statistics(model, validation, outputs[validation]))
        v = results[name]['validation']
        print(f'{name}: held-out exact {v["exact_fraction"]:.4%}, '
              f'max {v["max_absolute_error"]:.3g}, {v["max_ulp"]} ulps', flush=True)
    # Prefer the compact 64-segment quadratic for exhaustive evaluation. Larger
    # tables are comparisons, not selected on the exhaustive holdout results.
    selected = dict(models)['segments_64_degree_2']
    (directory / 'model.json').write_text(json.dumps(selected, indent=2) + '\n')
    # Exhaustive holdout excludes training and model-selection offsets.
    used = training.copy()
    used[validation] = True
    totals = dict(n=0, exact=0, within_1_ulp=0, max_ulp=0, max_absolute_error=0)
    bins = {}
    for start in range(0, Q + 1, 1 << 18):
        test = numpy.arange(start, min(start + (1 << 18), Q + 1))
        test = test[~used[test]]
        if not len(test):
            continue
        stats = statistics(selected, test, outputs[test])
        totals['n'] += stats['n']; totals['exact'] += stats['exact']
        totals['within_1_ulp'] += round(stats['n'] * stats['within_1_ulp_fraction'])
        totals['max_ulp'] = max(totals['max_ulp'], stats['max_ulp'])
        if stats['max_absolute_error'] > totals['max_absolute_error']:
            for key in ('max_absolute_error', 'worst_offset', 'got', 'hardware'):
                totals[key] = stats[key]
        distance = numpy.abs(evaluate(selected, test).astype(numpy.float32).view(numpy.uint32).astype(numpy.int64)
                             - outputs[test].astype(numpy.int64))
        for label, mask in (('first_segment', test < Q // 64), ('remaining_segments', test >= Q // 64)):
            group = bins.setdefault(label, dict(n=0, exact=0, max_ulp=0))
            group['n'] += int(mask.sum()); group['exact'] += int(numpy.count_nonzero(distance[mask] == 0))
            if mask.any(): group['max_ulp'] = max(group['max_ulp'], int(distance[mask].max()))
    totals['exact_fraction'] = totals['exact'] / totals['n']
    totals['within_1_ulp_fraction'] = totals['within_1_ulp'] / totals['n']
    full_domain = {}
    for name, model in (('ideal_sine', dict(kind='ideal_sine')), ('segments_64_degree_2', selected)):
        total = dict(n=0, exact=0, within_1_ulp=0, max_ulp=0, max_absolute_error=0)
        square_error = 0.0
        for start in range(0, Q + 1, 1 << 18):
            test = numpy.arange(start, min(start + (1 << 18), Q + 1))
            stats = statistics(model, test, outputs[test])
            total['n'] += stats['n']; total['exact'] += stats['exact']
            total['within_1_ulp'] += round(stats['n'] * stats['within_1_ulp_fraction'])
            total['max_ulp'] = max(total['max_ulp'], stats['max_ulp'])
            square_error += stats['n'] * stats['rms_absolute_error'] ** 2
            if stats['max_absolute_error'] > total['max_absolute_error']:
                for key in ('max_absolute_error', 'worst_offset', 'got', 'hardware'):
                    total[key] = stats[key]
        total['exact_fraction'] = total['exact'] / total['n']
        total['within_1_ulp_fraction'] = total['within_1_ulp'] / total['n']
        total['rms_absolute_error'] = float(numpy.sqrt(square_error / total['n']))
        full_domain[name] = total
    segment_results = []
    for segment in range(64):
        test = numpy.arange(segment * (Q // 64), (segment + 1) * (Q // 64)
                            + (1 if segment == 63 else 0))
        segment_results.append(dict(segment=segment, **statistics(selected, test, outputs[test])))
    (directory / 'segments.json').write_text(json.dumps(segment_results, indent=2) + '\n')
    first = selected['coefficients'][0]
    gain = first[1] * selected['segments'] / (numpy.pi / 2) - 1
    small = numpy.arange(1, 128)
    step = float(values(outputs[1:2])[0])
    report = dict(metadata=metadata, numpy_version=numpy.__version__, training_offsets=len(offsets),
        validation_offsets=len(validation), seed='0x53494e', results=results,
        selected_model='segments_64_degree_2', exhaustive_holdout=totals, holdout_regions=bins,
        full_reduced_domain=full_domain,
        exact_segments=[s['segment'] for s in segment_results if s['exact'] == s['n']],
        best_segment=max(segment_results, key=lambda s: s['exact_fraction']),
        first_segment_gain_relative_to_sine=float(gain), hardware_step=step,
        hardware_step_gain_relative_to_sine=float(step / numpy.sin(2 * numpy.pi / TURN) - 1),
        small_offsets_max_relative_step_error=float(numpy.max(numpy.abs(values(outputs[small]) / step - small))),
        interpretation='Coefficients fitted only to earlier reduction observations. Validation and exhaustive '
        'holdout are disjoint from training and each other. Float64 polynomial evaluation, then binary32 '
        'rounding; not a reconstruction of internal hardware arithmetic or coefficient storage.')
    (directory / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(exhaustive_holdout=totals, first_segment_gain=float(gain)), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'fit'))
    parser.add_argument('--out', type=Path, default=DEFAULT)
    args = parser.parse_args()
    if args.action == 'capture': capture(args.out)
    else: fit(args.out)


if __name__ == '__main__':
    main()
