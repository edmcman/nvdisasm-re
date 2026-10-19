"""Compare shifted segment grids and plot SM89 sine evaluator residuals.

PYTHONPATH=. uv run --no-project --python 3.11 --with numpy --with matplotlib \
  --with scipy python research/mufu/sin_structure.py
Reads the saved GPU table; does not execute GPU instructions. Model comparisons
use the same disjoint training/validation offsets for every candidate partition.
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['svg.hashsalt'] = 'sm89-mufu-sine-residuals'
import matplotlib.pyplot
import numpy
import scipy.optimize

from research.mufu import sin_fit

OUT = sin_fit.DEFAULT
Q = sin_fit.Q


def fit_shifted(offsets, y, width, shift):
    last = (Q - 1 + shift) // width
    indices = numpy.minimum((offsets + shift) // width, last)
    coefficients = []
    for segment in range(last + 1):
        mask = indices == segment
        t = (offsets[mask] + (shift if segment else 0)) / width - segment
        first = 1 if segment == 0 else 0
        matrix = numpy.stack([t ** power for power in range(first, 3)], axis=1)
        values = numpy.linalg.lstsq(matrix, y[mask], rcond=None)[0].tolist()
        coefficients.append(([0.0] if segment == 0 else []) + values)
    return dict(width=width, shift=shift, coefficients=coefficients)


def evaluate_shifted(model, offsets):
    last = len(model['coefficients']) - 1
    indices = numpy.minimum((offsets + model['shift']) // model['width'], last)
    t = (offsets + numpy.where(indices == 0, 0, model['shift'])) / model['width'] - indices
    c = numpy.array(model['coefficients'])[indices]
    return (c[:, 2] * t + c[:, 1]) * t + c[:, 0]


def metrics(predicted, words):
    rounded = predicted.astype(numpy.float32)
    distance = numpy.abs(rounded.view(numpy.uint32).astype(numpy.int64) - words.astype(numpy.int64))
    return dict(n=len(words), exact=int(numpy.count_nonzero(distance == 0)),
        exact_fraction=float(numpy.mean(distance == 0)), max_ulp=int(distance.max()),
        within_1_ulp_fraction=float(numpy.mean(distance <= 1)),
        max_absolute_error=float(numpy.max(numpy.abs(rounded.astype(numpy.float64) - sin_fit.values(words)))))


def quadratic_feasibility(outputs, model):
    # Segment 22 stays in one output binade, so all rounding cells have the same
    # ulp. Work in ulp units to avoid the solver's absolute-tolerance pitfalls.
    segment = 22
    offsets = numpy.arange(segment * 131072, (segment + 1) * 131072)
    y = sin_fit.values(outputs[offsets])
    assert numpy.all(numpy.spacing(y.astype(numpy.float32)) == 2.0 ** -24)
    t = offsets / 131072 - segment
    baseline = evaluate_shifted(model, offsets)
    residual = (y - baseline) * 2 ** 24
    matrix = numpy.stack((numpy.ones_like(t), t, t*t), axis=1)
    # Minimize the extra allowance beyond each output's +/-0.5 ulp rounding
    # interval. Closed intervals deliberately include ties, favoring feasibility.
    constraints = numpy.concatenate((matrix, -matrix))
    constraints = numpy.column_stack((constraints, -numpy.ones(len(constraints))))
    bounds = numpy.concatenate((residual + 0.5, -residual + 0.5))
    result = scipy.optimize.linprog([0, 0, 0, 1], A_ub=constraints, b_ub=bounds,
        bounds=[(None, None)] * 3 + [(0, None)], method='highs',
        options=dict(primal_feasibility_tolerance=1e-9, dual_feasibility_tolerance=1e-9))
    if not result.success:
        raise RuntimeError(result.message)
    return dict(segment=segment, n=len(offsets), extra_allowance_ulp=float(result.x[3]),
        solver='scipy.optimize.linprog / HiGHS', status=result.message,
        interpretation='Positive extra allowance means no real quadratic followed only by nearest '
        'float32 rounding satisfies all observed rounding intervals, to solver numerical tolerance. '
        'This does not exclude a quadratic with intermediate fixed-point rounding/truncation.')


def envelope(axis, x, y, block, **kwargs):
    count = len(x) // block * block
    xx = x[:count].reshape(-1, block).mean(axis=1)
    yy = y[:count].reshape(-1, block)
    axis.fill_between(xx, yy.min(axis=1), yy.max(axis=1), alpha=.22,
                      color=kwargs.get('color', 'C0'), linewidth=0)
    axis.plot(xx, yy.mean(axis=1), linewidth=.8, **kwargs)


def plots(outputs, original):
    figure, axes = matplotlib.pyplot.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    r = numpy.arange(Q + 1)
    real = numpy.sin((numpy.pi / 2) * (r / Q))
    error = (sin_fit.values(outputs) - real) * 1e9
    envelope(axes[0, 0], r / Q, error, 1024)
    axes[0, 0].set(title='GPU minus ordinary sine, entire reduced quarter turn',
        xlabel='Reduced offset / 2^23', ylabel='Absolute error × 10⁹')
    for boundary in range(1, 64):
        axes[0, 0].axvline(boundary / 64, color='gray', alpha=.15, linewidth=.6)
    stop = 4 * 131072
    envelope(axes[0, 1], r[:stop] / 131072, error[:stop], 128)
    axes[0, 1].set(title='First four candidate segments', xlabel='Reduced offset / 131072',
                   ylabel='GPU minus ordinary sine × 10⁹')
    for boundary in range(1, 4):
        axes[0, 1].axvline(boundary, color='gray', linestyle='--', linewidth=.8)
    del real, error, r
    r = numpy.arange(22 * 131072, 23 * 131072)
    residual = (sin_fit.values(outputs[r]) - sin_fit.evaluate(original, r)) * 2 ** 24
    envelope(axes[1, 0], r / 131072 - 22, residual, 128)
    # Jittered sample avoids accidentally hiding repeating low-bit structure by
    # plotting only a regularly spaced subset.
    sample = numpy.random.default_rng(22).choice(len(r), 3000, replace=False)
    axes[1, 0].scatter(r[sample] / 131072 - 22, residual[sample], s=1, alpha=.15)
    axes[1, 0].set(title='Segment 22: GPU minus unrounded fitted quadratic',
        xlabel='Position within segment 22', ylabel='Error in output ulps')
    axes[1, 0].axhline(0, color='gray', linewidth=.6)
    r = numpy.arange(4096)
    y = sin_fit.values(outputs[r])
    linear = r * sin_fit.values(outputs[1:2])[0]
    axes[1, 1].plot(r, (y - linear) * 1e12, linewidth=.7)
    axes[1, 1].set(title='Near zero: departure from the measured linear step',
        xlabel='Reduced integer offset r', ylabel='GPU minus r × hardware step, × 10¹²')
    for axis in axes.flat:
        axis.grid(alpha=.15)
    figure.suptitle('SM89 MUFU.SIN residuals — bands show block min/max', fontsize=13)
    figure.savefig(OUT / 'residuals.png', dpi=180)
    figure.savefig(OUT / 'residuals.svg', metadata={'Date': None})
    vector = OUT / 'residuals.svg'
    vector.write_text('\n'.join(line.rstrip() for line in vector.read_text().splitlines()) + '\n')
    matplotlib.pyplot.close(figure)


def main():
    outputs, metadata = sin_fit.read_table(OUT)
    original = json.loads((OUT / 'model.json').read_text())
    training = numpy.arange(32, Q, 64)
    y = sin_fit.values(outputs[training])
    candidates = numpy.unique(numpy.concatenate((
        numpy.random.default_rng(0x424f554e44).integers(0, Q + 1, 100000),
        numpy.arange(4096), numpy.arange(0, Q + 1, 131072))))
    validation = candidates[(candidates % 64 != 32)]
    comparisons = []
    dense_model = None
    for segments in (32, 64, 128):
        width = Q // segments
        for shift in sorted({0, 1, 2, 16, 128, width // 8, width // 4, width // 2, 3 * width // 4, 7 * width // 8}):
            model = fit_shifted(training, y, width, shift)
            result = metrics(evaluate_shifted(model, validation), outputs[validation])
            comparisons.append(dict(nominal_segments=segments, width=width, shift=shift, **result))
            if segments == 64 and shift == 0:
                dense_model = model
            print(f'{segments} segments shift {shift}: exact {result["exact_fraction"]:.3%}', flush=True)
    feasibility = quadratic_feasibility(outputs, dense_model)
    report = dict(output_sha256=metadata['output_sha256'], numpy_version=numpy.__version__,
        scipy_version=scipy.__version__, matplotlib_version=matplotlib.__version__, training_offsets=len(training),
        training_definition='r=32+64*k, 0<=r<2^23', validation_offsets=len(validation),
        seed='0x424f554e44', original_sparse_fit=metrics(sin_fit.evaluate(original, validation), outputs[validation]),
        comparisons=comparisons, segment_22_feasibility=feasibility,
        note='Exploratory comparisons on an already inspected dataset; training and validation indices '
        'are disjoint, not a newly blinded hardware experiment. Width/shift are in reduced integer offsets.')
    (OUT / 'structure.json').write_text(json.dumps(report, indent=2) + '\n')
    (OUT / 'dense-model.json').write_text(json.dumps(dense_model, indent=2) + '\n')
    plots(outputs, original)
    print(json.dumps(feasibility, indent=2))


if __name__ == '__main__':
    main()
