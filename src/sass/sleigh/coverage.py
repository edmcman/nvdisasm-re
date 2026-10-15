"""Summarize generated, per-class semantic coverage without hiding fallbacks."""
import argparse,html,json,re,subprocess,sys
from collections import Counter
from pathlib import Path
from sass.paths import LANGUAGES,ROOT
from sass.semantics.operations import OPERATION_FAMILIES

EVIDENCE=('gpu','ptxas','reference')
REFRESH='uv run sass-refresh-coverage'

def evidence(directory):
    """{arch: {class: {kind: ...}}} from the sass_smXX_evidence.json that tests/evidence.py writes."""
    out={}
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        arch=path.stem.removeprefix('sass_').removesuffix('_coverage').split('_')[0]
        source=path.with_name(f'sass_{arch}_evidence.json')
        if not source.exists():raise SystemExit(f'{source} is missing: run {REFRESH}')
        out[arch.upper()]=json.loads(source.read_text())
    return out

def verified(rows,found):
    """Classes with semantics among `rows` per evidence kind, and those with none."""
    rows=[r for r in rows if r['status']!='opaque']
    counts={kind:sum(kind in found.get(r['cls'],{}) for r in rows) for kind in EVIDENCE}
    return {**{k:n for k,n in counts.items() if n},**({'none':n} if (n:=sum(not found.get(r['cls']) for r in rows)) else {})}

def summarize(directory):
    out={};found=evidence(directory)
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        rows=json.loads(path.read_text());arch=path.stem.removeprefix('sass_').removesuffix('_coverage').upper()
        families={}
        for op in sorted(OPERATION_FAMILIES):
            group=[r for r in rows if r['opcode']==op]
            families[op]=dict(Counter(r['status'] for r in group))
            if not group:families[op]={'absent':0}
        ev=found[arch.split('_')[0]];ops=sorted({r['opcode'] for r in rows})
        out[arch]=dict(classes=len(rows),opcodes={op:dict(Counter(r['status'] for r in rows if r['opcode']==op)) for op in ops},status=dict(Counter(r['status'] for r in rows)),operations=families,
                      opaque_operations={r['cls']:r['reason'] for r in rows if r['opcode'] in OPERATION_FAMILIES and r['status']=='opaque'},
                      evidence=verified(rows,ev),opcode_evidence={op:v for op in ops if (v:=verified([r for r in rows if r['opcode']==op],ev))})
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
EVIDENCE_KEY={
    'gpu':('G','an SM89 GPU fixture that passed on hardware, exactly as committed, checks it: the instruction is on the fixture kernel’s path from the test inputs to the compared output.'),
    'ptxas':('P','ptxas emits it for an annotated PTX probe in <code>tests/ptx</code>: evidence of the lowering, not of values.'),
    'reference':('R','emulator cases check it against an independent reference in <code>tests/refs</code>.')}
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

def cell(counts,ev):
    if not counts:return '<td class="absent"></td>'
    covered,total=fraction(counts)
    tip=', '.join(f'{s} {counts[s]}' for s in STATUSES if s in counts)
    if ev:tip+='; classes with semantics verified by '+', '.join(f'{k} {ev[k]}' for k in (*EVIDENCE,'none') if k in ev)
    marks=''.join(f'<b class="ev-{k}">{EVIDENCE_KEY[k][0]}</b>' for k in EVIDENCE if k in ev)
    return f'<td class="heat" {heat(covered,total)} data-ev="{" ".join(ev)}" title="{tip}">{covered}/{total}<span class="marks">{marks}</span>{strip(counts)}</td>'

def opcode_cell(opcodes):
    if not opcodes:return '<td class="absent"></td>'
    covered=sum(set(c)!={'opaque'} for c in opcodes);total=len(opcodes)
    return f'<td class="heat" {heat(covered,total)} title="opcodes with semantics">{covered}/{total}</td>'

def card(arch,entry):
    covered,total=fraction(entry['status']);ops=entry['opcodes'].values()
    return (f'<div class="card heat" data-arch="{arch}" role="button" tabindex="0" {heat(opcode_percent(entry),100)}><h2>{arch}</h2><div class="big">{opcode_percent(entry)}%</div>'
            f'<div class="sub">{sum(set(c)!={"opaque"} for c in ops)}/{len(ops)} opcodes</div>'
            f'<div class="sub">{covered}/{total} classes</div>'
            f'<div class="sub">verified: '+(' · '.join(f'{n} {k}' for k in EVIDENCE if (n:=entry['evidence'].get(k))) or 'none')+f'</div>{strip(entry["status"])}</div>')

def report(summary):
    """Self-contained HTML: per-architecture cards and an opcode × architecture grid grouped by category."""
    archs=architectures(summary);of,order=categories()
    ops=sorted({op for a in archs for op in summary[a]['opcodes']},key=lambda op:(order.index(category(op,of)),op))
    def section(name):
        members=[op for op in ops if category(op,of)==name]
        row=''.join(opcode_cell([summary[a]['opcodes'][op] for op in members if op in summary[a]['opcodes']]) for a in archs)
        return (f'<tbody><tr class="category"><th>{html.escape(name)}</th>{row}</tr>\n'
                +''.join(f'<tr class="op"><th>{html.escape(op)}</th>'+''.join(cell(summary[a]['opcodes'].get(op),summary[a]['opcode_evidence'].get(op,{})) for a in archs)+'</tr>\n' for op in members)
                +'</tbody>\n')
    return REPORT.format(
        cards=''.join(card(a,summary[a]) for a in archs),
        head=''.join(f'<th data-arch="{a}" role="button" tabindex="0" title="Show only opcodes on {a}">{a}</th>' for a in archs),
        sections=''.join(section(c) for c in order if any(category(op,of)==c for op in ops)),
        key=''.join(f'<dt><i style="background:var(--{s})"></i>{s}</dt><dd>{d}</dd>' for s,d in STATUSES.items()),
        evidence=''.join(f'<dt><b class="ev-{k}">{m}</b>{k}</dt><dd>{d}</dd>' for k,(m,d) in EVIDENCE_KEY.items()),
        options=''.join(f'<option value="{k}">{k}</option>' for k in (*EVIDENCE,'none')))

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
[data-arch]{{cursor:pointer;user-select:none}} th[data-arch]:hover,th.active{{color:var(--fg);text-decoration:underline 2px}}
.card.active{{outline:3px solid var(--fg);outline-offset:-3px}} .cards:has(.active) .card:not(.active){{opacity:.55}}
.marks{{position:absolute;top:1px;right:2px;display:flex;gap:1px}}
b[class^=ev-]{{display:inline-block;font:700 8px/11px system-ui,sans-serif;min-width:11px;padding:0 2px;border-radius:3px;color:#fff;text-align:center}}
.key b[class^=ev-]{{font-size:10px;line-height:14px;min-width:14px;margin-right:6px}}
.ev-gpu{{background:#3949ab}} .ev-ptxas{{background:#6d4c41}} .ev-reference{{background:#546e7a}}
select{{font:inherit;color:var(--fg);background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:7px 9px;margin-left:8px}}
</style></head><body><main>
<h1>SASS semantic coverage</h1>
<p>How much of each architecture’s instruction set the generated Ghidra languages give semantics. A cell shows <b>classes with semantics / machine-description classes</b> for an opcode; its bottom edge shows the split by status. Category rows count opcodes with any semantics. Categories follow <a href="https://docs.nvidia.com/cuda/cuda-binary-utilities/index.html#instruction-set-reference">NVIDIA’s SASS reference</a>.</p>
<dl class="key">{key}</dl>
<p>Marks in a cell say how its classes with semantics are verified (per class in <code>evidence.json</code>):</p>
<dl class="key">{evidence}</dl>
<p>Classes with semantics are native or primitive. A cell’s colour goes from red when none of its classes have semantics to green when all do. Generated by <code>sass-refresh-coverage</code>; hover a cell for exact counts, click an architecture to show only its opcodes.</p>
<div class="cards">{cards}</div>
<div class="tools"><input id="q" type="search" placeholder="Filter opcodes…" aria-label="Filter opcodes"><select id="ev" aria-label="Filter by evidence"><option value="">any evidence</option>{options}</select></div>
<div class="wrap"><table>
<thead><tr><th>opcode</th>{head}</tr></thead>
{sections}</table></div>
</main>
<script>
const q=document.getElementById('q'),ev=document.getElementById('ev'),archs=[...document.querySelectorAll('thead th[data-arch]')].map(th=>th.dataset.arch);let arch=null;
function apply(){{const text=q.value.trim().toUpperCase(),col=archs.indexOf(arch)+1;
  const has=td=>(td.dataset.ev||'').split(' ').includes(ev.value);
  for(const r of document.querySelectorAll('tr.op'))r.hidden=!r.firstChild.textContent.includes(text)||(col>0&&r.children[col].classList.contains('absent'))
    ||ev.value&&!(col>0?has(r.children[col]):[...r.children].slice(1).some(has));
  for(const b of document.querySelectorAll('tbody'))b.hidden=!b.querySelector('tr.op:not([hidden])');
  for(const e of document.querySelectorAll('[data-arch]'))e.classList.toggle('active',e.dataset.arch===arch)}}
function pick(e){{const t=e.target.closest('[data-arch]');if(!t)return;arch=arch===t.dataset.arch?null:t.dataset.arch;apply()}}
document.addEventListener('click',pick);
document.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' '){{if(e.target.closest('[data-arch]')){{e.preventDefault();pick(e)}}}}}});
q.addEventListener('input',apply);ev.addEventListener('change',apply);
</script>
</body></html>
"""

def write(directory,output):
    """The summary of the manifests and evidence in `directory`, and beside it the per-class evidence, badge and report."""
    summary=summarize(directory);output=Path(output);output.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    output.with_name('evidence.json').write_text(json.dumps(evidence(directory),indent=1,sort_keys=True)+'\n')
    output.with_name('badge.json').write_text(json.dumps(badge(summary),ensure_ascii=False)+'\n')
    output.with_name('coverage.html').write_text(report(summary))

def main():
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--output');a=p.parse_args()
    if a.output:write(a.directory,a.output)
    else:print(json.dumps(summarize(a.directory),indent=2,sort_keys=True))

def refresh():
    """sass-refresh-coverage [SMxx ...]: regenerate the manifests (no Ghidra) and evidence, then the committed summary, evidence, badge and report."""
    for module in ('sass.sleigh.build','--generate-only'),('tests.evidence',):
        subprocess.run([sys.executable,'-m',*module,*sys.argv[1:]],cwd=ROOT,check=True)
    write(LANGUAGES,ROOT/'processor/SASS/semantics/coverage.json')

if __name__=='__main__':main()
