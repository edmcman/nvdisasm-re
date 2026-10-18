"""Packed conversion and half-lane adapters, backed by independent test references."""
from sass.semantics.fp import softfloat
from tests.harness import autocase_profiles, autocase_extended, autocase_half_profiles
from tests.harness.case import source
from tests.refs import float, integer

PACK_OPS={'F2IP','I2IP'}
HALF_OPS={'HADD2','HMUL2','HFMA2','HMNMX2','HSET2','HSETP2'}
OPS=PACK_OPS | HALF_OPS
FAMILIES={op:'conversions' if op in PACK_OPS else 'fp16' for op in OPS}
SWIZZLES={'H1_H0','H0_H0','H1_H1'}


def blocked_reason(op,form):
    if op not in HALF_OPS:return None
    if any(n.endswith(('@absolute','@negate')) and v=='set' for n,v in form.items()):
        return 'packed source-sign numerical corpus remains manual'
    if any(n.startswith('iswz') and v not in SWIZZLES for n,v in form.items()):
        return 'mixed-precision and sign-swizzle lane corpus remains manual'
    if form.get('xorsign','noxorsign')!='noxorsign' or 'isA' in form:
        return 'min/max XORSIGN and source-predicate outputs remain manual'
    if form.get('satrelu')=='RELU':return 'predicate-controlled half RELU remains manual'
    if op in ('HADD2','HMUL2','HFMA2') and form.get('ofmt','F16_V2')!='F16_V2':
        return 'mixed-format packed arithmetic remains manual'
    if form.get('Dfmt.Afmt.Bfmt','F16x2.F16x2.F16x2')!='F16x2.F16x2.F16x2':
        return 'mixed-format packed multiply remains manual'
    return None


def operand(s,name,value,reg):
    """Packed immediates name the high half first and the low half with a `1` suffix."""
    from tests.harness import autocases
    if name is None:raise autocases.Unsupported('unsupported packed operand layout')
    if name+'1' in s.k.operand_types:
        s.fields[name]=value>>16; s.fields[name+'1']=value&0xffff
    else:s.operand((name,),value,reg)
    return value


def lanes(word,swizzle):
    lo,hi=word&0xffff,word>>16
    return {'H1_H0':(lo,hi),'H0_H0':(lo,lo),'H1_H1':(hi,hi)}[swizzle]


def generate(c,k,form,profiles):
    from tests.harness import autocases
    expected=['packed_conversion_forms'] if k.mnemonic in PACK_OPS else ['half_comparison_forms'] if k.mnemonic in ('HSET2','HSETP2') else ['half_forms']
    if profiles!=expected:raise autocases.Unsupported('incompatible packed profiles')
    if k.mnemonic in PACK_OPS:return conversions(c,k,form)
    reason=blocked_reason(k.mnemonic,form)
    if reason:raise autocases.Unsupported(reason)
    return half(c,k,form)


def conversions(c,k,form):
    from tests.harness import autocases
    op=k.mnemonic
    allowed={'dstfmt','srcfmt','rnd','ntz','relu','extract','extract_limited','extract_dist_limited','extract_24','extract_28','satrelu','sat','SAT'}
    if set(form)-allowed:raise autocases.Unsupported('unhandled packed conversion modifiers')
    dst=form['dstfmt']; width=int(dst[1:]); signed=dst.startswith('S')
    if op=='F2IP' and (dst not in ('S8','U8') or form.get('rnd')!='ROUND' or form.get('ntz')!='NTZ' or form.get('relu')!='norelu' or form.get('extract','H0')!='H0'):
        raise autocases.Unsupported('F2IP profile owns ROUND.NTZ without RELU and H0 preservation')
    if op=='I2IP' and (width not in (2,4,8,16) or form.get('srcfmt','S32')!='S32'):
        raise autocases.Unsupported('unsupported integer pack format')
    spec=autocase_profiles.PROFILES['packed_conversion_forms']
    values=spec['floats'] if op=='F2IP' else spec['integers']
    for j,a in enumerate(values):
        b=values[(j+3)%len(values)]; preserved=(spec['preserved']+j*0x10203)&0xffffffff
        s=autocases.Inputs(c,k,form)
        names=[source(k,'Ra'),source(k,'Sb','Rb','URb'),source(k,'Sc','Rc','URc')]
        for n,value,reg in zip(names,(a,b,preserved),(4,6,8)):
            if op=='I2IP' and width==16 and n=='Rc':s.fields[n]='RZ'
            elif op=='F2IP' and n!=names[2] and k.operand_types[n].type!='SImm':autocase_extended.operand(s,n,value,reg,32)
            else:s.operand((n,),value,reg)
        if op=='F2IP':result=(preserved&0xffff)<<16 | float.f2ip_byte(a,signed)<<8 | float.f2ip_byte(b,signed)
        else:
            mode=form.get('satrelu',form.get('SAT',form.get('sat','SAT' if 'SAT' in k.operand_types else 'nosatrelu')))
            if mode not in ('nosatrelu','nosatrelu_relu','SAT','SATRELU','RELU'):
                raise autocases.Unsupported('unknown integer pack saturation')
            extract=next((form[n] for n in ('extract','extract_dist_limited','extract_limited','extract_24','extract_28') if n in form),'H0')
            result=integer.i2ip(integer.sext(a,32),integer.sext(b,32),preserved,width,signed,mode in ('SAT','SATRELU'),mode in ('RELU','SATRELU'),extract)
        out=s.output(('Rd',),4 if j&1 else 2)
        s.add({out:result},form,portable=True)
    return c


def half(c,k,form):
    from tests.harness import autocases
    op=k.mnemonic
    allowed={'ofmt','Dfmt.Afmt.Bfmt','ftz','fmz','sat','satrelu','nan','xorsign','cmp','bop','bval','h_and'}
    if any(n not in allowed and not n.startswith('iswz') and not n.endswith(('@absolute','@negate','@not')) for n in form):
        raise autocases.Unsupported('unhandled packed floating modifiers')
    fmt=form.get('ofmt','F16_V2').removesuffix('_V2')
    if fmt not in ('F16','BF16','E6M9') or op in ('HSET2','HSETP2') and fmt=='E6M9':
        raise autocases.Unsupported('unsupported packed lane format')
    names=[source(k,'Ra'),source(k,'Sb','Rb','URb'),source(k,'Sc','Rc','URc')]
    if op in ('HADD2','HSET2','HSETP2'):names=[names[0],names[2]]
    elif op!='HFMA2':names=names[:2]
    if not all(names):raise autocases.Unsupported('unsupported half source layout')
    swizzles=['iswzA']
    if op=='HFMA2':swizzles += [next((n for n in ('iswzB','iswzC_as_B') if n in form),None),next((n for n in ('iswzC','iswzB_as_C') if n in form),None)]
    else:swizzles += [next((n for n in ('iswzB_as_C','iswzB') if n in form),None)]
    values=autocase_profiles.PROFILES['half_forms'][fmt]
    samples=autocase_half_profiles.select(op,form,c.fp) if op in ('HSET2','HSETP2') else ((j,j&1) for j in range(len(values)))
    for j,raw_predicate in samples:
        a=values[j]
        s=autocases.Inputs(c,k,form)
        raw=(a,values[(j+3)%len(values)],values[(j+5)%len(values)])
        pairs=[lanes(operand(s,n,v,r),form.get(sw,'H1_H0')) for n,v,r,sw in zip(names,raw,(4,6,8),swizzles)]
        packed=[lo | hi<<16 for lo,hi in pairs]
        p=raw_predicate; pred=source(k,'Pp')
        if pred:
            s.predicate((pred,),p,0);p ^= form.get(pred+'@not')=='set'
        ftz=form.get('ftz')=='FTZ'; exact=c.fp=='exact'
        if op in ('HADD2','HMUL2','HFMA2'):
            operation={'HADD2':'add','HMUL2':'mul','HFMA2':'fma'}[op]
            sat=form.get('sat',form.get('satrelu','nosat'))=='SAT'
            mode=form.get('fmz','nofmz')
            if mode not in ('nofmz','nofmz_hfma2','FMZ','FTZ'):
                raise autocases.Unsupported('unknown packed flush mode')
            results=[]
            for lane in (0,1):
                xs=[pair[lane] for pair in pairs]
                if exact:
                    if op=='HMUL2' and mode=='FMZ' and any(x&0x7c00==0 for x in xs):result=0
                    else:result=softfloat.arithmetic(operation,xs,16,ftz=ftz or mode in ('FTZ','FMZ'),sat=sat)
                    if softfloat.unpack(result,16)[0]=='nan' or float.underflow_band(operation,xs,16):break
                else:result=float.readable(operation,xs,16,sat)
                if result is None:break
                results.append(result)
            if len(results)!=2:continue
            result=results[0] | results[1]<<16
        elif op=='HMNMX2':
            result=float.half_minmax(*packed,bool(p),form.get('nan')=='NAN',ftz,fmt=fmt)
        else:
            kw=dict(cmp=form['cmp'],bop=form['bop'],p=bool(p),ftz=exact and ftz,fmt=fmt)
            if op=='HSET2':result=float.hset2(*packed,bf=form['bval']=='BF',**kw)
            else:
                pu,pv=float.hsetp2(*packed,h_and=form['h_and']=='H_AND',**kw)
                expected={s.output(('Pu',),0):int(pu),s.output(('Pv',),1):int(pv)}
                s.add(expected,form,portable=True);continue
        out=s.output(('Rd',),4 if j&1 else 2)
        s.add({out:result},form,portable=True)
    return c
