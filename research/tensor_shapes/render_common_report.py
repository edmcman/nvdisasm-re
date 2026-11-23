import argparse
from html import escape
import json
from pathlib import Path


ALIASES = {
    'kc_axpy': ({'cb210': 'y', 'cb218': 'x'}, {'cb224:32': 'N'}),
    'kc_sgemm_naive': ({'cb210': 'C', 'cb218': 'A', 'cb220': 'B'}, {'cb230:32': 'M', 'cb234:32': 'N', 'cb238:32': 'K'}),
    'kc_sgemm_tiled': ({'cb210': 'C', 'cb218': 'A', 'cb220': 'B'}, {'cb230:32': 'M', 'cb234:32': 'N', 'cb238:32': 'K'}),
    'kc_gemv_n_simple': ({'cb210': 'A', 'cb218': 'x', 'cb220': 'y'}, {'cb230:32': 'M', 'cb234:32': 'N'}),
    'kc_dot_partial': ({'cb210': 'x', 'cb218': 'y', 'cb220': 'partial'}, {'cb228:32': 'N'}),
    'kc_strsm_lower_8x8': ({'cb210': 'L', 'cb218': 'X'}, {}),
}


def render(manifest):
    rows, recovered, total = [], 0, 0
    for entry in manifest['entries']:
        name, report = entry['entry'], entry.get('shapes', {})
        buffers, symbols = ALIASES.get(name, ({'cb210': 'input', 'cb218': 'output'}, {'cb220:32': 'N'}))
        symbols = {**symbols, 'cbc:32': 'grid.x'}
        tensors = report.get('tensors', [])
        cells = []
        for tensor in tensors:
            total += 1
            done = tensor['dimensions_recovered']
            recovered += done
            dimensions = [symbols.get(d, d) for d in tensor['dimensions']]
            if not done:
                prefix = 0
                for index in range(len(dimensions)):
                    checks = [c for c in tensor['coordinate_checks'] if c['dimension'] == index]
                    if not checks or not all(c['upper_bound_proven'] and c['nonnegative_proven'] for c in checks): break
                    prefix += 1
                dimensions = dimensions[:prefix] + ['?']
            label = buffers.get(tensor['buffer'], tensor['buffer'])
            signature = f'{label}[{",".join(dimensions)}]'
            cells.append(f'<div><code>{escape(signature)}</code> <span class="{"recovered" if done else "open"}">{"recovered, conditional" if done else "unresolved"}</span></div>')
        if not cells: cells = ['Unresolved']
        detail = {'arguments': tensors, 'residuals': report.get('residuals', []),
                  'fixed_point_verified': report.get('fixed_point_verified', False)}
        rows.append(f'<tr><th scope="row">{escape(name)}</th><td>{"".join(cells)}</td><td><details><summary>Evidence</summary><pre>{escape(json.dumps(detail, indent=2))}</pre></details></td></tr>')
    tests = manifest['tests']
    method = ('SASS → def/use and SSA → structural type unification → one equality/inequality fixed point → dimension constraints.')
    rules = Path(__file__).with_name('constraint_rules.html').read_text()
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tensor dimensions from SASS</title><style>
:root{{color-scheme:light;--ink:#19312d;--muted:#526860}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f5ee;color:var(--ink);font:16px/1.6 system-ui,sans-serif}}main{{max-width:1120px;margin:auto;padding:48px 24px}}h1{{font-size:clamp(2rem,5vw,3.4rem);line-height:1.1;margin:12px 0 24px}}h2{{margin-top:40px}}p{{max-width:850px}}code,pre{{font-family:ui-monospace,monospace}}code{{font-size:.9em}}table{{width:100%;border-collapse:collapse;background:#fffef9}}th,td{{text-align:left;padding:18px;border-bottom:1px solid #d7dfd7;vertical-align:top}}th{{font-weight:550}}thead{{background:#e6ece3}}td div+div{{margin-top:10px}}.table{{overflow-x:auto}}.recovered,.open{{font-size:.8rem;display:block;color:#286b47}}.open{{color:#975d20}}.note{{border-left:3px solid #ae762b;padding:12px 20px;background:#fff0d9}}.muted{{color:var(--muted)}}details{{max-width:400px}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.75rem;max-height:420px;overflow:auto}}a{{color:#175d45}}@media(max-width:700px){{th,td{{padding:10px}}th{{overflow-wrap:anywhere;max-width:150px}}}}
.rules pre{{font-size:.9rem;max-height:none;background:#e6ece3;padding:14px 16px;border-radius:4px}}.rules pre code{{font-size:1em}}
</style></head><body><main>
<p class="muted">Tensor shape analysis · 2026-11-06</p><h1>Tensor dimensions from SASS</h1>
<p>{recovered} of {total} pointer arguments have a complete dimension decomposition under the conditional integer model. All {len(manifest['entries'])} kernel entries are listed below.</p>
<p><a href="#constraint-rules">Constraint rules and worked examples</a></p>
<p class="note">All recovered dimensions are conditional. Integer overflow, signed extensions and address wrapping still need a complete proof. These are dimensions sufficient to describe the observed accesses; original source rank and allocation size are not uniquely determined.</p>
<div class="table"><table><thead><tr><th>Kernel</th><th>Tensor arguments</th><th>Details</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">M, N, K and argument names are presentation labels from the source fixtures. The analysis receives SASS and explicit input ranges, without those labels. Dimensions follow physical element-stride order. “?” marks an unresolved dimension or bound.</p>
<h2>How the analysis works</h2><p>{method}</p>
<p>Every edge uses the same parallel SSA assignment, guard intersection and predecessor join. A finite polynomial feature basis retains symbolic strides. Linear algebra derives equalities; Z3 projects linear inequalities. A generic interval/congruence analysis supplies scalar bounds. Loop headers widen until the analysis reaches a verified fixed point. There are no countdown, unroll-factor or tile recognizers in this path.</p>
<p>Dimensions come from common coordinate bounds and algebraic stride decomposition. Unsupported operations remain unknown and incomplete results stay visible.</p>
<p>The DOT kernel uses an approximate reciprocal to compute an integer quotient. Numerical constraints carry its error bound through the correction steps. Auxiliary loop counters retain relationships such as <code>index = start + 4 × stride × iterations</code>. These use common arithmetic rules, without kernel-name rules.</p>
<p class="note">DOT additionally assumes the documented reciprocal error bound applies to its SM90 MUFU.RCP operation: at most one ulp, the spacing between neighboring floating-point values. The numerical model bounds the approximation; it does not reproduce the reciprocal’s exact bits.</p>
{rules}
<h2>Validation</h2><p>{tests['run']} tests; {tests['failures']} failures, {tests['errors']} errors, {tests['skipped']} skips. The suite covers type unification, SSA and local calls, shared-memory types, the common constraint solver, renamed parameters, varied loop increments, contradictory paths, missing/negative bounds, reciprocal arithmetic, GEMM, DOT, triangular solve, and concrete SASS addresses and scalar bounds.</p>
<p><a href="../research/tensor_shapes/common_results/manifest.json">Exact results, contexts and source hashes</a> · <a href="shapes.md">Implementation notes</a> · <a href="../research/tensor_shapes/README.md">Run the analyzer</a></p>
</main></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.manifest.read_text())))


if __name__ == '__main__': main()
