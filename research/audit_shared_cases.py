"""Read-only reduction audit: preserve form coverage and modeled fault witnesses.

Run from the repository root:
    PYTHONPATH=. python -m research.audit_shared_cases OUTPUT.json

This does not alter plans, profiles, references, or execution comparisons.
"""
import json
from collections import Counter,defaultdict
from functools import cache
from pathlib import Path
from tests.harness import autocase_profiles,autocases
from tests.refs import float

SWIZZLES=('H1_H0','H0_H0','H1_H1')
CMPS=('F','LT','EQ','LE','GT','NE','GE','NUM','NAN','LTU','EQU','LEU','GTU','NEU','GEU','T')
SUBSETS=sorted((s for s in range(1,256)),key=lambda s:(s.bit_count(),s))
def lane_word(word,swizzle):
 lo,hi=word&65535,word>>16
 xs={'H1_H0':(lo,hi),'H0_H0':(lo,lo),'H1_H1':(hi,hi)}[swizzle]
 return xs[0]|xs[1]<<16

@cache
def conditions(fmt,ftz,cmp,sa,sb,j,swap=False):
 words=autocase_profiles.PROFILES['half_forms'][fmt]
 a,b=words[j],words[(j+3)%8]
 if swap:a,b=b,a
 return tuple(float.half_compares(lane_word(a,sa),lane_word(b,sb),cmp,ftz,fmt))

@cache
def result(sig,j,p_override=None,swap=False,reverse=False):
 op,fmt,ftz,cmp,bop,pnot,sa,sb,output=sig
 xs=conditions(fmt,ftz,cmp,sa,sb,j,swap)
 p=bool((j&1) if p_override is None else p_override)^pnot
 if op=='HSETP2' and output=='H_AND':
  both=all(xs);xs=(both,not both)
 xs=tuple(bool(float.BOPS[bop](x,p)) for x in xs)
 if reverse:xs=xs[::-1]
 if op=='HSETP2':return xs
 one=float.HALF_ONE[fmt] if output=='BF' else 65535
 return (one if xs[0] else 0)|((one if xs[1] else 0)<<16)

def replace(sig,i,v):return sig[:i]+(v,)+sig[i+1:]
def minimum_cover(masks):
 target=0
 for m in masks:target|=m
 for subset in SUBSETS:
  union=0
  for i,m in enumerate(masks):
   if subset>>i&1:union|=m
  if union==target:return [i for i in range(8) if subset>>i&1]

def fault_outputs(sig,samples):
 op,fmt,ftz,cmp,bop,pnot,sa,sb,output=sig
 def outcomes(changed=sig,override=None,swap=False,reverse=False):
  return tuple(result(changed,j,p_override=p if override is None else override,
                      swap=swap,reverse=reverse) for j,p in samples)
 variants={}
 for c in CMPS:
  if c!=cmp:variants['comparison:'+c]=outcomes(replace(sig,3,c))
 for b in ('AND','OR','XOR'):
  if b!=bop:variants['combiner:'+b]=outcomes(replace(sig,4,b))
 for axis,index,current in (('A',6,sa),('B',7,sb)):
  for sw in SWIZZLES:
   if sw!=current:variants['swizzle:'+axis+':'+sw]=outcomes(replace(sig,index,sw))
 variants['predicate:invert']=outcomes(replace(sig,5,not pnot))
 for stuck in (0,1):variants['predicate:stuck:'+str(stuck)]=outcomes(override=stuck)
 variants['sources:swapped']=outcomes(swap=True)
 variants['outputs:swapped']=outcomes(reverse=True)
 alt='BM' if output=='BF' else 'BF' if op=='HSET2' else 'noh_and' if output=='H_AND' else 'H_AND'
 variants['output_mode:wrong']=outcomes(replace(sig,8,alt))
 if fmt=='F16':variants['FTZ:toggle']=outcomes(replace(sig,2,not ftz))
 altfmt='BF16' if fmt=='F16' else 'F16'
 variants['format:wrong']=outcomes(replace(replace(sig,1,altfmt),2,False))
 return variants

def input_table():
 """Derive input-only selections from the independent reference-level audit."""
 table={}
 for path in autocases.PLANS.glob('SM*.json'):
  if '.gaps.' in path.name:continue
  for e in json.loads(path.read_text())['forms']:
   op='HSETP2' if e['cls'].startswith('hsetp2__') else 'HSET2' if e['cls'].startswith('hset2__') else None
   if not op:continue
   for fp in ('exact','readable'):
    sig=signature(op,e['settings'],fp);a=audit(sig)
    vectors=[[j,j&1] for j in a['selected']]
    if a['hidden_ftz_witness']:
     x=a['hidden_ftz_example'];vectors.append([x['seed_index'],x['raw_predicate']])
    key='/'.join(str(int(v)) if isinstance(v,bool) else v for v in sig)
    if key in table:assert table[key]==vectors
    table[key]=vectors
 return dict(version=1,inputs={fmt:autocase_profiles.PROFILES['half_forms'][fmt] for fmt in ('F16','BF16')},forms=table)

def write_inputs(path):
 data=input_table();table=data['forms']
 lines=['{','  "version": 1,','  "inputs": '+json.dumps(data['inputs'],sort_keys=True)+',','  "forms": {']
 lines+=['    '+json.dumps(k)+': '+json.dumps(v,separators=(',',':'))+(',' if i+1<len(table) else '') for i,(k,v) in enumerate(sorted(table.items()))]
 lines+=['  }','}']
 path.parent.mkdir(parents=True,exist_ok=True)
 path.write_text('\n'.join(lines)+'\n')

@cache
def audit(sig):
 op,fmt,ftz,cmp,bop,pnot,sa,sb,output=sig
 gold=tuple(result(sig,j) for j in range(8))
 variants=fault_outputs(sig,tuple((j,j&1) for j in range(8)))
 masks=[1<<(j&1) for j in range(8)] # Preserve both raw predicate values and HSET2 alias/separate placement.
 for bit,outputs in enumerate(variants.values(),2):
  for j,(a,b) in enumerate(zip(gold,outputs)):
   if a!=b:masks[j]|=1<<bit
 chosen=minimum_cover(masks)
 assert chosen and set(j&1 for j in chosen)=={0,1}
 for outputs in variants.values():
  assert any(a!=b for a,b in zip(gold,outputs))==any(gold[j]!=outputs[j] for j in chosen)
 masked=sum((bop=='AND' and not (bool(j&1)^pnot)) or (bop=='OR' and (bool(j&1)^pnot)) for j in range(8))
 insensitive=sum(cmp in ('F','T') or ((bop=='AND' and not (bool(j&1)^pnot)) or (bop=='OR' and (bool(j&1)^pnot))) for j in range(8))
 # Find FTZ tests hidden solely by the correlation between numeric seed and predicate.
 hidden=False;example=None
 if fmt=='F16' and ftz and variants.get('FTZ:toggle')==gold:
  other=replace(sig,2,False)
  for j in range(8):
   p=1-(j&1)
   a,b=result(sig,j,p_override=p),result(other,j,p_override=p)
   if a!=b:
    hidden=True;example={'seed_index':j,'raw_predicate':p,'expected_ftz':a,'expected_noftz':b};break
 return dict(selected=chosen,masked_cases=masked,numeric_insensitive_cases=insensitive,
             observed_faults=sum(outputs!=gold for outputs in variants.values()),
             unobserved_faults=[name for name,outputs in variants.items() if outputs==gold],
             hidden_ftz_witness=hidden,hidden_ftz_example=example)

def signature(op,f,mode):
 return (op,f.get('ofmt','F16_V2').removesuffix('_V2'),mode=='exact' and f.get('ftz')=='FTZ',
         f['cmp'],f['bop'],f.get('Pp@not')=='set',f.get('iswzA','H1_H0'),
         f.get('iswzB',f.get('iswzB_as_C','H1_H0')),f.get('bval',f.get('h_and')))
def main():
 import argparse
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('output',type=Path)
 parser.add_argument('--write-inputs',type=Path,help='write the audited input-only profile table')
 args=parser.parse_args()
 rows=[];examples=[];total=Counter()
 for path in sorted(autocases.PLANS.glob('SM*.json')):
  if '.gaps.' in path.name:continue
  arch=path.stem
  groups=Counter()
  for e in json.loads(path.read_text())['forms']:
   op='HSETP2' if e['cls'].startswith('hsetp2__') else 'HSET2' if e['cls'].startswith('hset2__') else None
   if not op:continue
   assert e['profiles'] in (['half_forms'],['half_comparison_forms'])
   for mode in ('exact','readable'):groups[(mode,signature(op,e['settings'],mode))]+=1
  stats=defaultdict(Counter)
  for (mode,sig),n in groups.items():
   a=audit(sig);op=sig[0];st=stats[(op,mode)]
   st.update(forms=n,current_cases=8*n,candidate_cases=len(a['selected'])*n,
             masked_cases=a['masked_cases']*n,numeric_insensitive_cases=a['numeric_insensitive_cases']*n,
             hidden_ftz_forms=n if a['hidden_ftz_witness'] else 0)
   st['forms_reduced_to_'+str(len(a['selected']))]+=n
   if a['hidden_ftz_witness'] and len(examples)<6:
    examples.append(dict(arch=arch,mode=mode,signature=sig,**a))
  for (op,mode),s in sorted(stats.items()):
   rows.append(dict(arch=arch,op=op,mode=mode,**s));total.update({k:s[k] for k in ('forms','current_cases','candidate_cases','masked_cases','numeric_insensitive_cases','hidden_ftz_forms')})
  print(arch,'audited',sum(s['current_cases'] for s in stats.values()),'cases',flush=True)
 out=dict(scope='Generated HSET2/HSETP2 only; every saved form and both lowerings retained',
          criteria='Preserve both raw predicate values, HSET2 alias/separate placement, and every currently observed modeled-fault distinction per form. Numeric corpus is unchanged. Minimum subset searched exhaustively over 255 nonempty subsets.',
          limits='This is mutation-based evidence, not proof of equivalent bug detection. Unobserved faults include genuinely equivalent behaviors. Operand classes remain distinct; no architecture or lowering is skipped. Results are an audit proposal, not an applied suite change.',
          totals=dict(total),rows=rows,examples=examples,unique_semantic_audits=audit.cache_info().currsize)
 dest=args.output;dest.write_text(json.dumps(out,indent=2)+'\n')
 if args.write_inputs:write_inputs(args.write_inputs)
 print('TOTAL',dict(total),flush=True)
if __name__=='__main__':main()
