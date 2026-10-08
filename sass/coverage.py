"""Summarize generated, per-class semantic coverage without hiding fallbacks."""
import argparse,json
from collections import Counter
from pathlib import Path
from sass.tierb import TIER_B

def summarize(directory):
    out={}
    for path in sorted(Path(directory).glob('sass_sm*_coverage.json')):
        rows=json.loads(path.read_text());arch=path.stem.split('_')[1].upper()
        families={}
        for op in sorted(TIER_B):
            group=[r for r in rows if r['opcode']==op]
            families[op]=dict(Counter(r['status'] for r in group))
            if not group:families[op]={'absent':0}
        out[arch]=dict(classes=len(rows),status=dict(Counter(r['status'] for r in rows)),tier_b=families,
                      opaque_tier_b={r['cls']:r['reason'] for r in rows if r['opcode'] in TIER_B and r['status']=='opaque'},hardware_verified=False)
    return out

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory');p.add_argument('--output');a=p.parse_args()
    value=json.dumps(summarize(a.directory),indent=2,sort_keys=True)+'\n'
    if a.output:Path(a.output).write_text(value)
    else:print(value,end='')
