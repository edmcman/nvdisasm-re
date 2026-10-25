"""Semantics and evidence gaps of one opcode's modifier combinations, or a ranking of opcodes by them.

uv run python .agents/skills/sass-opcode-coverage/scripts/gaps.py            # rank SM89 opcodes
uv run python .agents/skills/sass-opcode-coverage/scripts/gaps.py FADD       # architectures with equal counts grouped, then SM89 classes

Before/after changes: python3 .github/scripts/coverage_comment.py local [REV].
"""
import argparse,json
from collections import Counter
from pathlib import Path
from sass.paths import LANGUAGES,ROOT
from sass.sleigh import coverage

COVERAGE=ROOT/'processor/SASS/semantics/coverage.json'
EXAMPLES=4

def verified(arch):
    """The evidence kind that counts as verification: hardware exists only for SM89."""
    return 'gpu' if arch.startswith('SM89') else 'reference'

def totals(entry,op):
    return Counter(entry['combination_status'].get(op,{})),Counter(entry['combination_evidence'].get(op,{}))

def line(name,status,ev,kind):
    n=sum(status.values()) or 1
    partial=sum(v for k,v in status.items() if k.startswith('partially'))
    return (f"{name:<10} {n:>7} combos | native {status['native']:>7} primitive {status['primitive']:>6} opaque {status['opaque']:>7} partial {partial:>5}"
            f" | gpu {ev['gpu']:>6} ref {ev['reference']+ev['ptxas']:>6} none {ev['none']:>7}"
            f" | modelled {status['native']/n:4.0%} verified {ev[kind]/n:4.0%}")

def rank(cov,arch,top,everything):
    entry=cov[arch];kind=verified(arch);rows=[]
    for op in entry['combination_status']:
        status,ev=totals(entry,op);n=sum(status.values())
        if everything or status['opaque']<n:rows.append((n-min(status['native'],ev[kind]),op,status,ev))
    print(f'{arch}: opcodes by modifier combinations not both native and {kind}-verified')
    for gap,op,status,ev in sorted(rows,reverse=True)[:top]:
        if gap:print(f'{gap:>7}',line(op,status,ev,kind))

def classes(op,arch,directory):
    path=directory/f'sass_{arch.lower()}_coverage.json'
    if not path.exists():raise SystemExit(f'{path} is missing: uv run sass-build-languages {arch} --fp exact --generate-only --coverage (or --manifest-dir)')
    found=json.loads(path.with_name(f'sass_{arch.lower()}_evidence.json').read_text());kind=verified(arch)
    for r in (r for r in coverage.manifest(path) if r['opcode']==op):
        ev=found.get(r['cls'],{});status,part=coverage.combinations(r,ev);c=r.get('combinations') or {}
        print(f"\n{r['cls']}  [{r['status']}] {r.get('reason','')}\n  ",line('',Counter(status),Counter(part),kind))
        gaps={}
        for *labs,s in c.get('table',[]):
            if s!='native':gaps.setdefault(s,[]).append(dict(zip(c['decides'],labs)))
        for s,examples in gaps.items():print(f"   {s} x{len(examples)} (x{c['free']} free each): {examples[:EXAMPLES]}")
        for m,lab,_,kinds,_,_ in coverage.values(r,ev):
            if m and kind not in kinds:print(f"   never {kind}-verified: {m}={lab} (evidence {'/'.join(kinds) or 'none'})")

def main():
    p=argparse.ArgumentParser()
    p.add_argument('opcode',nargs='?')
    p.add_argument('--arch',default='SM89')
    p.add_argument('--top',type=int,default=30)
    p.add_argument('--all',action='store_true',help='rank fully opaque opcodes too')
    p.add_argument('--manifest-dir',type=Path,default=LANGUAGES)
    a=p.parse_args();cov=json.loads(COVERAGE.read_text())
    if not a.opcode:return rank(cov,a.arch,a.top,a.all)
    op=a.opcode.upper();same={}
    for arch in (k for k in cov if '_' not in k):same.setdefault(line('',*totals(cov[arch],op),verified(arch)),[]).append(arch)
    for text,archs in same.items():print(' '.join(sorted(archs,key=lambda s:int(s[2:]))),'\n  ',text)
    classes(op,a.arch,a.manifest_dir)

if __name__=='__main__':main()
