"""Summarize generated, per-class semantic coverage without hiding fallbacks."""
import argparse,html,json,re
from collections import Counter
from pathlib import Path
from sass.semantics.operations import OPERATION_FAMILIES

def summarize(directory):
    out={}
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        rows=json.loads(path.read_text());arch=path.stem.removeprefix('sass_').removesuffix('_coverage').upper()
        families={}
        for op in sorted(OPERATION_FAMILIES):
            group=[r for r in rows if r['opcode']==op]
            families[op]=dict(Counter(r['status'] for r in group))
            if not group:families[op]={'absent':0}
        out[arch]=dict(classes=len(rows),opcodes={op:dict(Counter(r['status'] for r in rows if r['opcode']==op)) for op in sorted({r['opcode'] for r in rows})},status=dict(Counter(r['status'] for r in rows)),operations=families,
                      opaque_operations={r['cls']:r['reason'] for r in rows if r['opcode'] in OPERATION_FAMILIES and r['status']=='opaque'},hardware_verified=False)
    return out

def color(percent):
    match percent:
        case p if p>=90:return 'brightgreen'
        case p if p>=75:return 'green'
        case p if p>=50:return 'yellowgreen'
        case p if p>=25:return 'yellow'
        case _:return 'orange'

def architectures(summary):
    """Exact variants only, numerically: readable ones have identical coverage."""
    return sorted((a for a in summary if '_' not in a),key=lambda a:int(re.sub(r'\D','',a)))

def opcode_percent(entry):
    """Opcodes with at least one native or primitive class."""
    ops=entry['opcodes'].values()
    return round(100*sum(set(c)!={'opaque'} for c in ops)/len(ops))

def badge(summary):
    """shields.io endpoint: the range of per-architecture opcode coverage."""
    archs=architectures(summary);percent=[opcode_percent(summary[a]) for a in archs]
    return dict(schemaVersion=1,label='opcode semantics',message=f'{min(percent)}–{max(percent)}% · {archs[0]}–{archs[-1]}',color=color(min(percent)))

STATUSES={
    'native':'P-code computes the result, so Ghidra’s decompiler and emulator see the real operation.',
    'primitive':'Calls a named runtime operation (<code>sass_prim_*</code>) that our emulator executes; the decompiler sees only a call.',
    'opaque':'Calls a generic operation with the machine description’s reads and writes but no semantics: dataflow only.'}
UNLISTED="Not in NVIDIA's reference"

def categories():
    """Opcode -> category from NVIDIA's SASS instruction set reference, in its order."""
    listed=json.loads(Path(__file__).with_name('categories.json').read_text())['categories']
    return {op:c for c,ops in listed.items() for op in ops},[*listed,UNLISTED]

def category(op,of):
    return of.get(op) or of.get(op.split('.')[0],UNLISTED)

def heat(covered,total):
    """A cell or card shaded red (nothing covered) through yellow to green (everything)."""
    return f'style="--h:{round(120*covered/total)}"'

def strip(counts):
    total=sum(counts.values());edges=[0]
    for s in STATUSES:edges.append(edges[-1]+100*counts.get(s,0)/total)
    stops=','.join(f'var(--{s}) {a:.1f}% {b:.1f}%' for s,a,b in zip(STATUSES,edges,edges[1:]))
    return f'<i class="strip" style="background:linear-gradient(90deg,{stops})"></i>'

def fraction(counts):
    total=sum(counts.values());return total-counts.get('opaque',0),total

def cell(counts):
    if not counts:return '<td class="absent"></td>'
    covered,total=fraction(counts)
    tip=', '.join(f'{s} {counts[s]}' for s in STATUSES if s in counts)
    return f'<td class="heat" {heat(covered,total)} title="{tip}">{covered}/{total}{strip(counts)}</td>'

def opcode_cell(opcodes):
    if not opcodes:return '<td class="absent"></td>'
    covered=sum(set(c)!={'opaque'} for c in opcodes);total=len(opcodes)
    return f'<td class="heat" {heat(covered,total)} title="opcodes with semantics">{covered}/{total}</td>'

def card(arch,entry):
    covered,total=fraction(entry['status']);ops=entry['opcodes'].values()
    return (f'<div class="card heat" {heat(opcode_percent(entry),100)}><h2>{arch}</h2><div class="big">{opcode_percent(entry)}%</div>'
            f'<div class="sub">{sum(set(c)!={"opaque"} for c in ops)}/{len(ops)} opcodes</div>'
            f'<div class="sub">{covered}/{total} classes</div>{strip(entry["status"])}</div>')

def report(summary):
    """Self-contained HTML: per-architecture cards and an opcode × architecture grid grouped by category."""
    archs=architectures(summary);of,order=categories()
    ops=sorted({op for a in archs for op in summary[a]['opcodes']},key=lambda op:(order.index(category(op,of)),op))
    def section(name):
        members=[op for op in ops if category(op,of)==name]
        row=''.join(opcode_cell([summary[a]['opcodes'][op] for op in members if op in summary[a]['opcodes']]) for a in archs)
        return (f'<tbody><tr class="category"><th>{html.escape(name)}</th>{row}</tr>\n'
                +''.join(f'<tr class="op"><th>{html.escape(op)}</th>'+''.join(cell(summary[a]['opcodes'].get(op)) for a in archs)+'</tr>\n' for op in members)
                +'</tbody>\n')
    return REPORT.format(
        cards=''.join(card(a,summary[a]) for a in archs),
        head=''.join(f'<th>{a}</th>' for a in archs),
        sections=''.join(section(c) for c in order if any(category(op,of)==c for op in ops)),
        key=''.join(f'<dt><i style="background:var(--{s})"></i>{s}</dt><dd>{d}</dd>' for s,d in STATUSES.items()))

REPORT="""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SASS semantic coverage</title>
<style>
:root{{--native:#43a047;--primitive:#f9a825;--opaque:#e53935;--fg:#1d2330;--muted:#5e6878;--bg:#f4f5f8;--panel:#fff;--rule:#e3e6eb;--s:75%;--l:84%}}
@media (prefers-color-scheme:dark){{:root{{--native:#66bb6a;--primitive:#fdd835;--opaque:#ef5350;--fg:#e6e9ef;--muted:#99a2b2;--bg:#11141a;--panel:#1a1e26;--rule:#2b313c;--s:45%;--l:26%}}}}
*{{box-sizing:border-box}}
body{{font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:28px 16px}}
main{{max-width:1180px;margin:auto}}
h1{{font-size:28px;margin:0 0 6px;letter-spacing:-.01em}} p{{color:var(--muted);max-width:80ch;margin:0 0 6px}} a{{color:inherit}}
.key{{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:16px 0 10px;max-width:90ch}} .key dt{{font-weight:600}} .key dd{{margin:0;color:var(--muted)}}
.key i{{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}}
.heat{{background:hsl(var(--h) var(--s) var(--l))}}
.cards{{margin-top:22px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:28px}}
.card{{position:relative;overflow:hidden;border-radius:12px;padding:12px 14px 16px;box-shadow:0 1px 2px rgb(0 0 0/.08)}}
.card h2{{font-size:12px;margin:0;letter-spacing:.06em;opacity:.75}} .big{{font-size:30px;font-weight:700;line-height:1.1}}
.sub{{font-size:12px;opacity:.8;font-variant-numeric:tabular-nums}}
.strip{{position:absolute;left:0;right:0;bottom:0;height:4px}}
.tools{{margin-bottom:10px}} input{{font:inherit;color:var(--fg);background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:7px 11px;width:min(300px,100%)}}
.wrap{{overflow:auto;max-height:82vh;background:var(--panel);border:1px solid var(--rule);border-radius:12px}}
table{{border-collapse:separate;border-spacing:3px;font-variant-numeric:tabular-nums;width:100%}}
th,td{{padding:4px 6px;white-space:nowrap}}
td{{position:relative;text-align:center;font-size:12px;font-weight:600;border-radius:5px;min-width:64px;overflow:hidden}}
td.absent{{background:repeating-linear-gradient(45deg,transparent 0 4px,var(--rule) 4px 5px)}}
thead th{{position:sticky;top:0;z-index:3;background:var(--panel);font-size:12px;color:var(--muted);padding:9px 6px}}
tbody th{{position:sticky;left:0;z-index:1;background:var(--panel);text-align:left;font:600 12px ui-monospace,SFMono-Regular,Menlo,monospace;padding-right:14px}}
thead th:first-child{{left:0;z-index:4;text-align:left}}
tr.category th{{font:700 13px system-ui,sans-serif;padding-top:16px}} tr.category td{{font-size:12px}}
tr.op:hover th{{color:var(--primitive)}}
</style></head><body><main>
<h1>SASS semantic coverage</h1>
<p>How much of each architecture’s instruction set the generated Ghidra languages give semantics. A cell shows <b>classes with semantics / machine-description classes</b> for an opcode; its bottom edge shows the split by status. Category rows count opcodes with any semantics. Categories follow <a href="https://docs.nvidia.com/cuda/cuda-binary-utilities/index.html#instruction-set-reference">NVIDIA’s SASS reference</a>.</p>
<dl class="key">{key}</dl>
<p>Classes with semantics are native or primitive. A cell’s colour goes from red when none of its classes have semantics to green when all do. Generated by <code>sass-coverage</code>; hover a cell for exact counts.</p>
<div class="cards">{cards}</div>
<div class="tools"><input id="q" type="search" placeholder="Filter opcodes…" aria-label="Filter opcodes"></div>
<div class="wrap"><table>
<thead><tr><th>opcode</th>{head}</tr></thead>
{sections}</table></div>
</main>
<script>
document.getElementById('q').addEventListener('input',e=>{{const q=e.target.value.trim().toUpperCase();
for(const r of document.querySelectorAll('tr.op'))r.hidden=!r.firstChild.textContent.includes(q)}});
</script>
</body></html>
"""

def write(summary,output):
    """The summary and, beside it, its badge and report."""
    output=Path(output);output.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    output.with_name('badge.json').write_text(json.dumps(badge(summary),ensure_ascii=False)+'\n')
    output.with_name('coverage.html').write_text(report(summary))

def main():
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--output');a=p.parse_args()
    summary=summarize(a.directory)
    if a.output:write(summary,a.output)
    else:print(json.dumps(summary,indent=2,sort_keys=True))

if __name__=='__main__':main()
