"""Summarize generated, per-class semantic coverage without hiding fallbacks."""
import argparse,base64,gzip,json,re,subprocess,sys
from collections import Counter
from pathlib import Path
from sass.paths import LANGUAGES,ROOT
from sass.sleigh.modifiers import form
from sass.semantics.operations import OPERATION_FAMILIES

EVIDENCE=('gpu','ptxas','reference')   # strongest first: a class or value counts once, under the first that ran it
PARTIAL={'partially native':'native','partially primitive':'primitive'}   # -> the modelled part
UNLISTED="Not in NVIDIA's reference"
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

def shown(row):
    """The class's status for the report: partially native (or primitive) when it has semantics but
    some of its modifier combinations are opaque or partial, i.e. some encodings fall back to the opaque call."""
    gaps=any(s=='opaque' or s in PARTIAL for *_,s in row.get('combinations',{}).get('table',[]))
    return {**row,'status':'partially '+row['status'] if row['status']!='opaque' and gaps else row['status']}

def manifest(path):
    return [shown(r) for r in json.loads(path.read_text())]

def strongest(kinds):
    """{kind: n} over evidence-kind lists, each counted once under its strongest kind, or none."""
    return dict(Counter(next((k for k in EVIDENCE if k in ks),'none') for ks in kinds))

def verified(rows,found):
    """Classes with semantics among `rows` by strongest evidence."""
    return strongest([k for k in EVIDENCE if k in found.get(r['cls'],{})] for r in rows if r['status']!='opaque')

def values(row,ev):
    """[(modifier, label, status, evidence kinds, combinations by status, tested combinations by
    strongest kind)] of a class's modifier choices; the class itself (modifier None) when it has none.
    A choice's evidence is the kinds whose forms ran it; its combinations are the class's legal ones
    that make it."""
    mods={m:v for m,v in row.get('modifiers',{}).items() if v}
    if not mods:return [(None,None,row['status'],[k for k in EVIDENCE if k in ev],{},{})]
    names=ev.get('modifiers',[]);c=row['combinations']
    forms={}
    for kind,fs in ev.get('forms',{}).items():
        for f in fs:forms.setdefault(tuple(f),set()).add(kind)
    def count(m,lab):
        i=c['decides'].index(m) if m in c['decides'] else None;out=Counter()
        for *labs,s in c['table']:
            if i is None or labs[i]==lab:out[s]+=c['free'] if i is not None else c['free']//len(c['labels'][m])
        return dict(out)
    def tested(m,lab):
        return strongest(ks for f,ks in forms.items() if dict(zip(names,f)).get(m)==lab)
    ran={k:{(m,lab) for f in fs for m,lab in zip(names,f)} for k,fs in ev.get('forms',{}).items()}
    return [(m,lab,status,[k for k in EVIDENCE if (m,lab) in ran.get(k,())],count(m,lab),tested(m,lab)) for m,vs in mods.items() for lab,status in vs.items()]

def combinations(row,ev):
    """(the class's modifier combinations by status, those with semantics by strongest evidence): a
    tested combination is a form the evidence ran, looked up in the class's table; the class itself is
    one combination when it has no modifiers."""
    c=row.get('combinations')
    if not c or not c['table'] or not any(row.get('modifiers',{}).values()):
        kinds=[k for k in EVIDENCE if k in ev];return Counter({row['status']:1}),Counter(strongest([kinds]) if row['status']!='opaque' else {})
    counts=Counter();lookup={tuple(labs):s for *labs,s in c['table']}
    for *_,s in c['table']:counts[s]+=c['free']
    names=ev.get('modifiers',[]);tested={}
    for kind,forms in ev.get('forms',{}).items():
        for f in forms:
            settings=dict(zip(names,f))
            if lookup.get(tuple(settings.get(m) for m in c['decides']),'opaque')!='opaque':tested.setdefault(tuple(f),set()).add(kind)
    part=Counter(strongest(tested.values()));part['none']=sum(counts.values())-counts['opaque']-len(tested)
    return counts,+part

def combination_summary(rows,found):
    """(modifier combinations by status, those with semantics by strongest evidence)."""
    status,part=Counter(),Counter()
    for r in rows:c,p=combinations(r,found.get(r['cls'],{}));status+=c;part+=p
    return dict(status),dict(part)

def summarize(directory):
    out={};found=evidence(directory)
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        rows=manifest(path);arch=path.stem.removeprefix('sass_').removesuffix('_coverage').upper()
        families={}
        for op in sorted(OPERATION_FAMILIES):
            group=[r for r in rows if r['opcode']==op]
            families[op]=dict(Counter(r['status'] for r in group))
            if not group:families[op]={'absent':0}
        ev=found[arch.split('_')[0]];ops=sorted({r['opcode'] for r in rows})
        by_op={op:[r for r in rows if r['opcode']==op] for op in ops};combos={op:combination_summary(g,ev) for op,g in by_op.items()}
        out[arch]=dict(classes=len(rows),opcodes={op:dict(Counter(r['status'] for r in g)) for op,g in by_op.items()},status=dict(Counter(r['status'] for r in rows)),operations=families,
                      opaque_operations={r['cls']:r['reason'] for r in rows if r['opcode'] in OPERATION_FAMILIES and r['status']=='opaque'},
                      evidence=verified(rows,ev),opcode_evidence={op:v for op in ops if (v:=verified(by_op[op],ev))},
                      combination_status={op:v[0] for op,v in combos.items()},combination_evidence={op:v[1] for op,v in combos.items() if v[1]})
    return out

def details(directory):
    """Per-class detail for the report: {"classes": [entry], "archs": {arch: {opcode: [entry index]}}},
    each class's entry (status, modifier values, tested combinations by kind) stored once across the
    architectures that share it."""
    found=evidence(directory);entries={};archs={}
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        arch=path.stem.removeprefix('sass_').removesuffix('_coverage').upper()
        if '_' in arch:continue
        for r in manifest(path):
            ev=found[arch].get(r['cls'],{});names=ev.get('modifiers',[])
            entry=json.dumps(dict(cls=r['cls'],status=r['status'],kinds=[k for k in EVIDENCE if k in ev],
                combinations=dict(combinations(r,ev)[0]),decides=r.get('combinations',{}).get('decides',[]),
                illegal={m:out for m,labs in r.get('combinations',{}).get('labels',{}).items() if (out:=[l for l in labs if l not in r['modifiers'].get(m,{})])},
                values=[list(v) for v in values(r,ev)],
                forms={k:[form(dict(zip(names,f))) for f in fs] for k,fs in ev.get('forms',{}).items()}),sort_keys=True)
            archs.setdefault(arch,{}).setdefault(r['opcode'],[]).append(entries.setdefault(entry,len(entries)))
    return dict(classes=[json.loads(e) for e in entries],archs=archs)

def packed(data):
    """Base64 gzip of compact JSON, which the report inflates with DecompressionStream."""
    return base64.b64encode(gzip.compress(json.dumps(data,separators=(',',':')).encode(),mtime=0)).decode()

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

def categories():
    """Opcode -> category from NVIDIA's SASS instruction set reference, in its order."""
    listed=json.loads(Path(__file__).with_name('categories.json').read_text())['categories']
    return {op:c for c,ops in listed.items() for op in ops},[*listed,UNLISTED]

def category(op,of):
    return of.get(op) or of.get(op.split('.')[0],UNLISTED)

def report(summary,detail):
    """Self-contained HTML: report/index.html with its CSS and JS inlined, rendering the embedded data."""
    archs=architectures(summary);of,order=categories()
    ops=sorted({op for a in archs for op in summary[a]['opcodes']},key=lambda op:(order.index(category(op,of)),op))
    data=dict(archs=archs,categories=[[c,members] for c in order if (members:=[op for op in ops if category(op,of)==c])],
              summary={a:{k:summary[a][k] for k in ('opcodes','combination_status','opcode_evidence','combination_evidence')} for a in archs},
              classes=detail['classes'],members=detail['archs'])
    page=Path(__file__).with_name('report')
    return ((page/'index.html').read_text().replace('__DATA__',packed(data))
            .replace('/*CSS*/',(page/'report.css').read_text()).replace('/*JS*/',(page/'report.js').read_text()))

def write(directory,output):
    """The summary of the manifests and evidence in `directory`, and beside it the per-class evidence, badge and report."""
    summary=summarize(directory);output=Path(output);output.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    found=evidence(directory)
    output.with_name('evidence.json').write_text('{\n'+',\n'.join(f'{json.dumps(a)}:{{\n'+',\n'.join(f'{json.dumps(c)}:{json.dumps(e,sort_keys=True)}' for c,e in sorted(found[a].items()))+'}' for a in sorted(found))+'}\n')
    output.with_name('badge.json').write_text(json.dumps(badge(summary),ensure_ascii=False)+'\n')
    output.with_name('coverage.html').write_text(report(summary,details(directory)))

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
