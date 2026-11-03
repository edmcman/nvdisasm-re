import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path

import numpy
from scipy.optimize import linprog
from scipy.spatial import HalfspaceIntersection, QhullError

from research.mufu import reciprocal_fit, reciprocal_observe


def disputed(capture, parity, limit, seed):
    segments = 128 if capture['op'] == 'RCP' else 64
    offsets, _, values, ulp, width, matrix = reciprocal_fit.domain(capture, parity, segments, 'partial_products')
    rounding = 'nearest_even' if capture['precision'] == 'F32' else 'toward_zero'
    target = values + (ulp / 2 if rounding == 'toward_zero' else 0)
    local = numpy.arange(1 << width, dtype=numpy.int64)
    scan = numpy.column_stack((numpy.ones(len(local)), local / 2 ** width,
                               reciprocal_fit.square_terms(local, width) / 32768))
    random = numpy.random.default_rng(seed)
    inputs, findings = [], []
    mantissa, bias = (23, 127) if capture['precision'] == 'F32' else (20, 1023)
    for segment in range(segments):
        selected = (offsets >> width == segment) & (offsets != 0)
        a, y, spacing = matrix[selected], target[selected], ulp[selected]
        center = numpy.linalg.lstsq(a, y, rcond=None)[0]
        scale = spacing.min()
        residual = (y - a @ center) / spacing
        normalized = a * scale / spacing[:, None]
        constraints = numpy.concatenate((normalized, -normalized))
        limits = numpy.concatenate((residual + .5, -residual + .5))
        options = dict(primal_feasibility_tolerance=1e-9, dual_feasibility_tolerance=1e-9)
        interior = linprog([0, 0, 0, -1], A_ub=numpy.column_stack((constraints, numpy.ones(len(constraints)))),
                           b_ub=limits, bounds=[(None, None)] * 4, method='highs', options=options)
        if not interior.success or interior.x[3] < -1e-7:
            raise ValueError(f"{capture['op']} {capture['precision']} parity {parity} segment {segment}: incompatible observations")
        try:
            vertices = HalfspaceIntersection(numpy.column_stack((constraints, -limits)), interior.x[:3]).intersections
        except QhullError:
            vertices = numpy.array([linprog(direction, A_ub=constraints, b_ub=limits,
                                            bounds=[(None, None)] * 3, method='highs', options=options).x
                                    for direction in numpy.concatenate((numpy.eye(3), -numpy.eye(3)))])
        predicted = scan @ center[:, None] + scale * (scan @ vertices.T)
        low, high = predicted.min(axis=1), predicted.max(axis=1)
        if rounding == 'nearest_even':
            low_words, high_words = low.astype(numpy.float32).view(numpy.uint32), high.astype(numpy.float32).view(numpy.uint32)
        else:
            low_words, high_words = low.view(numpy.uint64) >> 32, high.view(numpy.uint64) >> 32
        candidates = numpy.flatnonzero(low_words != high_words)
        candidates = candidates[(candidates + (segment << width)) != 0]
        candidates = candidates[~numpy.isin(candidates, offsets[selected] & ((1 << width) - 1))]
        chosen = random.choice(candidates, min(limit, len(candidates)), replace=False)
        inputs.extend(((bias + parity) << mantissa) | (segment << width) | int(x) for x in chosen)
        findings.append(dict(segment=segment, disputed=len(candidates), selected=len(chosen),
                             vertices=len(vertices), interior_margin_ulp=float(interior.x[3])))
    return inputs, findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('captures', type=Path, nargs='+')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=256)
    parser.add_argument('--seed', type=int, default=119)
    parser.add_argument('--tag', default='refined')
    args = parser.parse_args()
    groups = defaultdict(list)
    for path in args.captures:
        data = json.loads(gzip.decompress(path.read_bytes()))
        groups[data['arch'], data['op'], data['precision']].append((path, data))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for (arch, op, precision), items in groups.items():
        combined = dict(items[0][1], inputs=sum((d['inputs'] for _, d in items), []),
                        outputs=sum((d['outputs'] for _, d in items), []))
        values, findings = [], []
        for parity in (0, 1):
            inputs, segments = disputed(combined, parity, args.limit, args.seed)
            values += inputs
            findings.append(dict(parity=parity, segments=segments))
            print(op, precision, parity, len(inputs), 'targeted inputs', flush=True)
        name = f'{arch}-{op.lower()}-{precision}-{args.tag}'
        (args.output_dir / f'{name}-selection.json').write_text(json.dumps(dict(
            captures=[p.name for p, _ in items], seed=args.seed, limit=args.limit, findings=findings), indent=1) + '\n')
        if values:
            reciprocal_observe.capture(arch, op, precision, 'H0', values, args.output_dir / f'{name}.json.gz')


if __name__ == '__main__': main()
