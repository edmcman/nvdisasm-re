"""Scalar IEEE conversion form adapters using independent numerical references."""
from tests.harness import autocase_profiles, autocase_extended
from tests.harness.case import source, pair
from tests.refs import float

OPS = {'F2I','UF2I','I2F','I2FP','UI2F','UI2FP','F2F'}
MODES = {'ROUND':'RN','FLOOR':'RM','CEIL':'RP','TRUNC':'RZ','RN':'RN','RM':'RM','RP':'RP','RZ':'RZ'}


def generate(c,k,form,profiles):
    from tests.harness import autocases
    if profiles != ['conversion_forms','conversion_boundaries']: raise autocases.Unsupported('incompatible conversion profiles')
    allowed={'srcfmt','dstfmt','dstfmt.srcfmt','rnd','ftz','ntz','hsel','bsel'}
    if any(n not in allowed and not n.endswith(('@absolute','@negate')) for n in form):
        raise autocases.Unsupported('unhandled conversion modifiers')
    if any(n in k.operand_types for n in ('Pu','Pv')): raise autocases.Unsupported('conversion predicate outputs')
    op=k.mnemonic.removeprefix('U')
    if op=='F2F': dst,src=form['dstfmt.srcfmt'].split('.')
    else: src,dst=form['srcfmt'],form['dstfmt']
    floats={'F16','F32','F64'}; integers={s+str(w) for s in 'SU' for w in (8,16,32,64)}
    if (src not in (integers if op in ('I2F','I2FP') else floats)
            or dst not in (integers if op=='F2I' else floats)):
        raise autocases.Unsupported('conversion format outside scalar IEEE profile')
    sw,dw=int(src[1:]),int(dst[1:])
    if op=='F2I' and dw not in (32,64): raise autocases.Unsupported('narrow integer destinations remain unmodelled')
    if op=='F2F' and form.get('ftz','noftz')!='noftz': raise autocases.Unsupported('F2F FTZ remains unmodelled')
    name=source(k,'Sb','Rb','URb'); dest=source(k,'Rd','URd')
    if not name or not dest: raise autocases.Unsupported('unsupported conversion source layout')
    mode=MODES.get(form.get('rnd','RN'))
    if mode is None: raise autocases.Unsupported('unknown conversion rounding')
    spec=autocase_profiles.PROFILES['conversion_forms']
    values=spec['integers'] if op in ('I2F','I2FP') else spec['floats'][sw]
    dense=autocase_profiles.PROFILES['conversion_boundaries']
    if k.operand_types[name].type in ('Register','UniformRegister') and form.get('ftz','noftz')=='noftz' and not any(n.endswith(('@absolute','@negate')) and v=='set' for n,v in form.items()):
        extra=dense['integers'] if op in ('I2F','I2FP') else dense['f2f16'] if op=='F2F' and sw==16 else dense['floats'][sw]
        values=tuple(dict.fromkeys((*values,*extra)))
    for j,raw in enumerate(values):
        s=autocases.Inputs(c,k,form)
        value=raw & ((1<<sw)-1)
        selector=form.get('hsel',form.get('bsel'))
        shift=int(selector[1:])*sw if selector else 0
        word=(spec['filler'] & ~(((1<<sw)-1)<<shift)) | (value<<shift) if selector else value
        kind=k.operand_types[name].type
        if op in ('I2F','I2FP'):
            # Integer 64-bit immediates supply the high word, as the literal -1 case documents.
            encoded=(word>>32) if sw==64 and kind in ('SImm','UImm') else word
            s.operand((name,),encoded,4,size=max(sw,32)//8)
            effective=(encoded<<32) if sw==64 and kind in ('SImm','UImm') else word
            value=(effective>>shift)&((1<<sw)-1)
            result=float.i2f(value,sw,src.startswith('S'),dw,mode)
        else:
            effective=autocase_extended.operand(s,name,word,4,max(sw,32))
            value=(effective>>shift)&((1<<sw)-1)
            value=float.source_modifiers(value,sw,form.get(name+'@absolute')=='set',form.get(name+'@negate')=='set')
            if op=='F2I':
                result=float.f2i(value,sw,dw,dst.startswith('S'),mode if c.fp=='exact' else 'RZ',form.get('ntz')=='NTZ',form.get('ftz')=='FTZ')
            else:
                rounding=mode if c.fp=='exact' else 'RN'
                if c.fp=='readable' and float.conversion_underflow(value,sw,dw): continue
                result=float.f2f(value,sw,dw,rounding)
        out=s.output((dest,),4 if j%2 else 2)
        expected=pair(out,result) if dw==64 else {out:result}
        s.add(expected,form,portable=True)
    return c
