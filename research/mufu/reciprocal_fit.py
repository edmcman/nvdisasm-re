import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path

import numpy
from research.mufu import sin_quantization


def words(accumulators, precision, rounding, fraction_bits):
    mantissa, bias = (23, 127) if precision == 'F32' else (20, 1023)
    leading = numpy.frexp(accumulators.astype(numpy.float64))[1].astype(numpy.int64) - 1
    leading -= (1 << leading) > accumulators
    significand = (accumulators >> numpy.maximum(leading - mantissa, 0)) << numpy.maximum(mantissa - leading, 0)
    if rounding == 'nearest_even':
        shift = numpy.maximum(leading - mantissa, 1)
        remainder = accumulators & ((1 << shift) - 1)
        half = 1 << (shift - 1)
        significand += (leading > mantissa) & ((remainder > half) | ((remainder == half) & ((significand & 1) != 0)))
        carry = significand == 1 << (mantissa + 1)
        significand >>= carry.astype(numpy.int64)
        leading += carry
    return ((leading - fraction_bits + bias) << mantissa) | (significand & ((1 << mantissa) - 1))


def square_terms(local, width, retained=15):
    if retained == 15: return sin_quantization.square_products(local, width)
    cutoff = 2 * width - retained
    result = numpy.zeros(len(local), dtype=numpy.int64)
    for high in range(width):
        diagonal = 2 * high - cutoff
        if diagonal >= 0: result += ((local >> high) & 1) << diagonal
        for low in range(high):
            shift = high + low + 1 - cutoff
            if shift >= 0: result += (((local >> high) & (local >> low)) & 1) << shift
    return result


def domain(capture, parity, segments, square, retained=15):
    precision = capture['precision']
    mantissa, bias = (23, 127) if precision == 'F32' else (20, 1023)
    pairs = [(x & ((1 << mantissa) - 1), y) for x, y in zip(capture['inputs'], capture['outputs'], strict=True)
             if x >> mantissa == bias + parity]
    offsets = numpy.array([x for x, _ in pairs], dtype=numpy.int64)
    observed = numpy.array([y for _, y in pairs], dtype=numpy.uint32)
    if precision == 'F32':
        values = observed.view(numpy.float32).astype(numpy.float64)
        ulp = numpy.spacing(observed.view(numpy.float32)).astype(numpy.float64)
    else:
        values = (observed.astype(numpy.uint64) << 32).view(numpy.float64)
        ulp = ((observed.astype(numpy.uint64) + 1) << 32).view(numpy.float64) - values
    width = mantissa - (segments.bit_length() - 1)
    local = offsets & ((1 << width) - 1)
    match square:
        case 'partial_products': quadratic = square_terms(local, width, retained) / 2 ** retained
        case 'square_then_truncate': quadratic = (local * local >> (2 * width - retained)) / 2 ** retained
        case 'ordinary': quadratic = (local / 2.0 ** width) ** 2
    matrix = numpy.stack((numpy.ones(len(local)), local / 2.0 ** width, quadratic), axis=1)
    return offsets, observed, values, ulp, width, matrix


def fixed_accumulator(coefficients, local, width, grid):
    accumulator, constant, quadratic = grid
    squared = sin_quantization.square_products(local, width)
    return ((coefficients[..., 0] << (accumulator - constant)) + coefficients[..., 1] * local
            + ((coefficients[..., 2] * squared) << (accumulator - quadratic - 15)))


def fit(capture, parity, segments, square, rounding, grid):
    offsets, observed, values, ulp, width, matrix = domain(capture, parity, segments, square)
    target = values + (ulp / 2 if rounding == 'toward_zero' else 0)
    accumulator, constant, quadratic_bits = grid or (48, 48, 48)
    scales = numpy.array([2 ** constant, 2 ** (accumulator - width), 2 ** quadratic_bits])
    partition = offsets >> width
    predicted = numpy.empty(len(offsets), dtype=numpy.int64)
    training = numpy.zeros(len(offsets), dtype=bool)
    coefficients = []
    random = numpy.random.default_rng(22)
    for segment in range(segments):
        indices = numpy.flatnonzero(partition == segment)
        selected = random.choice(indices, len(indices) // 2, replace=False)
        training[selected] = True
        coefficient = numpy.linalg.lstsq(matrix[selected], target[selected], rcond=None)[0]
        if grid: coefficient = numpy.rint(coefficient * scales) / scales
        coefficients.append((numpy.rint(coefficient * scales).astype(numpy.int64).tolist() if grid else coefficient.tolist()))
        predicted[indices] = numpy.rint(matrix[indices] @ coefficient * 2 ** accumulator).astype(numpy.int64)
    got = words(predicted, capture['precision'], rounding, accumulator)
    distances = numpy.abs(got.astype(numpy.int64) - observed)
    def stats(mask):
        selected = distances[mask]
        return dict(n=len(selected), exact=int(numpy.count_nonzero(selected == 0)), max_ulp=int(selected.max()))
    return dict(segments=segments, square=square, final_rounding=rounding, coefficient_grid=grid,
                training=stats(training), holdout=stats(~training), coefficients=coefficients)


def intervals(capture, parity, square, rounding, retained=15, segments=None):
    from scipy.optimize import linprog
    segments = segments or (128 if capture['op'] == 'RCP' else 64)
    offsets, _, values, ulp, width, matrix = domain(capture, parity, segments, square, retained)
    target = values + (ulp / 2 if rounding == 'toward_zero' else 0)
    findings = []
    for segment in range(segments):
        selected = (offsets >> width == segment) & (offsets != 0)
        a, y, spacing = matrix[selected], target[selected], ulp[selected]
        center = numpy.linalg.lstsq(a, y, rcond=None)[0]
        residual = (y - a @ center) / spacing
        scale = spacing.min()
        normalized = a * scale / spacing[:, None]
        constraints = numpy.concatenate((normalized, -normalized))
        constraints = numpy.column_stack((constraints, -numpy.ones(len(constraints))))
        limits = numpy.concatenate((residual + .5, -residual + .5))
        result = linprog([0, 0, 0, 1], A_ub=constraints, b_ub=limits,
                         bounds=[(None, None)] * 3 + [(0, None)], method='highs',
                         options=dict(primal_feasibility_tolerance=1e-9, dual_feasibility_tolerance=1e-9))
        if not result.success: raise RuntimeError(result.message)
        findings.append(dict(segment=segment, minimum_extra_ulp=float(result.fun)))
    return dict(segments=segments, square=square, retained_square_bits=retained, final_rounding=rounding,
                feasible_segments=sum(f['minimum_extra_ulp'] < 1e-7 for f in findings),
                max_extra_ulp=max(f['minimum_extra_ulp'] for f in findings), segment_results=findings)


def integer_model(capture, parity, rounding, grid):
    from scipy.optimize import Bounds, LinearConstraint, linprog, milp
    segments = 128 if capture['op'] == 'RCP' else 64
    offsets, observed, values, ulp, width, matrix = domain(capture, parity, segments, 'partial_products')
    accumulator, constant, quadratic = grid
    scales = numpy.array([2 ** constant, 2 ** (accumulator - width), 2 ** quadratic])
    target = values + (ulp / 2 if rounding == 'toward_zero' else 0)
    coefficients, failures, details = [], [], []
    for segment in range(segments):
        selected = (offsets >> width == segment) & (offsets != 0)
        a, y, spacing = matrix[selected], target[selected], ulp[selected]
        center = numpy.linalg.lstsq(a, y, rcond=None)[0]
        residual = (y - a @ center) / spacing
        scale = spacing.min()
        normalized = a * scale / spacing[:, None]
        constraints = numpy.column_stack((numpy.concatenate((normalized, -normalized)),
                                          numpy.ones(2 * len(a))))
        limits = numpy.concatenate((residual + .5, -residual + .5))
        feasible = linprog([0, 0, 0, -1], A_ub=constraints, b_ub=limits,
                           bounds=[(None, None)] * 4, method='highs',
                           options=dict(primal_feasibility_tolerance=1e-9, dual_feasibility_tolerance=1e-9))
        if not feasible.success: raise RuntimeError(feasible.message)
        base = numpy.rint((center + feasible.x[:3] * scale) * scales)
        local = offsets[selected] & ((1 << width) - 1)
        rows = numpy.column_stack((numpy.full(len(local), 1 << (accumulator - constant)), local,
                                   square_terms(local, width) << (accumulator - quadratic - 15)))
        mantissa, bias = (23, 127) if capture['precision'] == 'F32' else (20, 1023)
        hardware = observed[selected].astype(numpy.int64)
        unit = 1 << (accumulator + (hardware >> mantissa) - bias - mantissa)
        lower = ((1 << mantissa) | (hardware & ((1 << mantissa) - 1))) * unit
        upper = lower + unit - 1
        if rounding == 'nearest_even':
            upper = lower + (unit >> 1) - (hardware & 1)
            lower -= (unit >> 1) - (hardware & 1)
        baseline = rows @ base.astype(numpy.int64)
        normalization = rows.max(axis=1)
        constraint = LinearConstraint(rows / normalization[:, None], (lower - baseline) / normalization,
                                      (upper - baseline) / normalization)
        result = milp(numpy.zeros(3), integrality=numpy.ones(3), bounds=Bounds(-256, 256),
                      constraints=constraint, options=dict(time_limit=1))
        if not result.success:
            failures.append(segment); coefficients.append(None)
            details.append(dict(segment=segment, status=result.message)); continue
        coefficient = base + numpy.rint(result.x)
        predicted = fixed_accumulator(coefficient.astype(numpy.int64), local, width, grid)
        got = words(predicted, capture['precision'], rounding, accumulator)
        if numpy.any(got != observed[selected]):
            failures.append(segment)
            details.append(dict(segment=segment, status='rounding mismatch',
                                mismatches=int(numpy.count_nonzero(got != observed[selected]))))
        coefficients.append(coefficient.astype(numpy.int64).tolist())
    return dict(segments=segments, final_rounding=rounding, coefficient_grid=grid,
                failed_segments=failures, failures=details, coefficients=coefficients)


def validate(capture, parity, model):
    if any(c is None for c in model['coefficients']): return dict(error='unresolved coefficients')
    offsets, observed, _, _, width, _ = domain(capture, parity, model['segments'], 'partial_products')
    selected = offsets != 0
    offsets, observed = offsets[selected], observed[selected]
    coefficients = numpy.array(model['coefficients'], dtype=numpy.int64)[offsets >> width]
    accumulator = fixed_accumulator(coefficients, offsets & ((1 << width) - 1), width, model['coefficient_grid'])
    got = words(accumulator, capture['precision'], model['final_rounding'], model['coefficient_grid'][0])
    failures = numpy.flatnonzero(got != observed)
    return dict(n=len(offsets), mismatches=len(failures), max_ulp=int(numpy.abs(got.astype(numpy.int64) - observed).max()),
                examples=[dict(offset=int(offsets[i]), got=int(got[i]), hardware=int(observed[i])) for i in failures[:5]])


def export_header(report, output):
    tables = []
    for precision in ('F32', '64H'):
        for op, parities in (('RCP', (0,)), ('RSQ', (0, 1))):
            coefficients = []
            for parity in parities:
                finding = next(f for f in report['findings'] if
                               (f['op'], f['precision'], f['exponent_parity']) == (op, precision, parity))
                model = next(m for m in finding['integer_models'] if
                             m['coefficient_grid'] == [57, 42, 40] and m['final_rounding'] ==
                             ('nearest_even' if precision == 'F32' else 'toward_zero'))
                if model['failed_segments']: raise ValueError('unresolved coefficients')
                coefficients.append('{\n' + ',\n'.join('  {' + ', '.join(map(str, row)) + '}'
                                                       for row in model['coefficients']) + '\n}')
            size = '[128][3]' if op == 'RCP' else '[2][64][3]'
            name = op + ('32' if precision == 'F32' else '64')
            tables.append(f'inline constexpr int64_t {name}{size} = ' +
                          (coefficients[0] if op == 'RCP' else '{\n' + ',\n'.join(coefficients) + '\n}') + ';')
    output.write_text('#pragma once\n#include <cstdint>\n\nnamespace sass::mufu {\n' + '\n\n'.join(tables) + '\n}\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('captures', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--intervals', action='store_true')
    parser.add_argument('--integer', action='store_true')
    parser.add_argument('--join', action='store_true')
    parser.add_argument('--model-only', action='store_true')
    parser.add_argument('--grid', type=int, nargs=3, action='append')
    parser.add_argument('--validate', type=Path, nargs='+', default=[])
    parser.add_argument('--export-header', type=Path)
    args = parser.parse_args()
    findings, groups = [], defaultdict(list)
    for path in args.captures:
        data = json.loads(gzip.decompress(path.read_bytes()))
        groups[(data['op'], data['precision']) if args.join else (path,)].append((path, data))
    validations = [(path, json.loads(gzip.decompress(path.read_bytes()))) for path in args.validate]
    grids = args.grid or ((40, 26, 24), (41, 26, 24), (42, 27, 25), (43, 28, 26),
                         (44, 29, 27), (45, 30, 28), (47, 32, 30), (51, 36, 34))
    for items in groups.values():
        path = items[0][0]
        data = dict(items[0][1], inputs=sum((d['inputs'] for _, d in items), []),
                    outputs=sum((d['outputs'] for _, d in items), []))
        for parity in (0, 1):
            models = [fit(data, parity, segments, square, rounding, grid)
                      for segments in (32, 64, 128) for square in ('ordinary', 'square_then_truncate', 'partial_products')
                      for rounding in (('nearest_even', 'toward_zero') if data['precision'] == 'F32' else ('toward_zero',))
                      for grid in (None, (37, 25, 22), (40, 26, 24), (41, 26, 24), (42, 27, 25), (43, 28, 26))] if not args.model_only else []
            best = max(models, key=lambda m: m['holdout']['exact']) if models else None
            if best: print(path.name, parity, {k: v for k, v in best.items() if k != 'coefficients'}, flush=True)
            feasibility = [intervals(data, parity, square, rounding)
                           for square in ('ordinary', 'square_then_truncate', 'partial_products')
                           for rounding in (('nearest_even', 'toward_zero') if data['precision'] == 'F32' else ('toward_zero',))] if args.intervals else []
            for model in feasibility:
                print('intervals', path.name, parity, {k: v for k, v in model.items() if k != 'segment_results'}, flush=True)
            integers = [integer_model(data, parity, rounding, grid)
                        for rounding in (('nearest_even', 'toward_zero') if data['precision'] == 'F32' else ('toward_zero',))
                        for grid in grids] if args.integer else []
            for model in integers:
                model['validation'] = [dict(capture=p.name, **validate(d, parity, model))
                                       for p, d in validations if (d['op'], d['precision']) == (data['op'], data['precision'])]
                print('integer', path.name, parity, model['final_rounding'], model['coefficient_grid'],
                      'failed', len(model['failed_segments']), 'validation', model['validation'], flush=True)
            findings.append(dict(capture=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 captures=[dict(name=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p, _ in items],
                                 op=data['op'], precision=data['precision'],
                                 exponent_parity=parity, selected=best,
                                 models=[{k: v for k, v in model.items() if k != 'coefficients'} for model in models],
                                 intervals=feasibility, integer_models=integers))
    report = dict(seed=22, grid_fields=['accumulator_bits', 'constant_bits', 'quadratic_bits'],
                  linear_bits='accumulator_bits-local_width', findings=findings)
    args.output.write_text(json.dumps(report, indent=1) + '\n')
    if args.export_header: export_header(report, args.export_header)


if __name__ == '__main__': main()
