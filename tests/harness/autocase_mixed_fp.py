"""Mixed half/binary32-pair form coverage; numerical results come from independent references."""
from tests.harness import autocase_profiles, autocase_extended
from tests.harness.case import pair, source
from tests.refs import float, fpformat, mixed

OPS={'FHADD2','FHMUL2','FHFMA2'}
FAMILIES={op:'fp32x2' for op in OPS}


def generate(c,k,form,profiles):
    from tests.harness import autocases
    if profiles!=['mixed_pair_forms']: raise autocases.Unsupported('incompatible mixed pair profile')
    allowed={'Afmt','Dfmt','rnd','ftz','fmz','iswzA','iswzB','iswzC','negA','negC'}
    if any(n not in allowed and not n.endswith(('@absolute','@negate')) for n in form):
        raise autocases.Unsupported('unhandled mixed floating modifiers')
    afmt={'F16x2':'F16','BF16x2':'BF16','F32x2':None}.get(form.get('Afmt','F32x2'),'unknown')
    dst={'F16x2':'F16','BF16x2':'BF16','F32x2':None}.get(form.get('Dfmt','F32x2'),'unknown')
    if 'unknown' in (afmt,dst): raise autocases.Unsupported('unknown mixed lane format')
    if form.get('fmz','nofmz_hfma2') not in ('nofmz_hfma2','FTZ'):
        raise autocases.Unsupported('mixed FMZ numerical semantics remain unresolved')
    op={'FHADD2':'add','FHMUL2':'mul','FHFMA2':'fma'}[k.mnemonic]
    names=[source(k,'Ra'),source(k,'Rb','URb','Sb'),source(k,'Rc','URc','Sc')]
    specs=list(zip(names,('iswzA','iswzB','iswzC'),('negA',None,'negC')))
    if op=='add':specs=[specs[0],specs[2]]
    elif op=='mul':specs=specs[:2]
    values=autocase_profiles.PROFILES['mixed_pair_forms'];exact=c.fp=='exact'
    for j in range(len(values['F32'])):
        s=autocases.Inputs(c,k,form);inputs=[]
        for i,(name,sw,neg) in enumerate(specs):
            if name is None:raise autocases.Unsupported('unsupported mixed operand layout')
            half=afmt if i==0 else None;pool=values[half or 'F32']
            lo,hi=pool[(j+2*i)%len(pool)],pool[(j+2*i+1)%len(pool)]
            selector=form.get(sw,'F32')
            typ=k.operand_types[name].type
            if half:
                if selector not in ('H1_H0','H0_H0','H1_H1'):raise autocases.Unsupported('unknown mixed half swizzle')
                value=lo|hi<<16;s.operand((name,),value,4+2*i)
            elif sw in form and selector!='F32':
                if selector not in ('F32x2.HI_LO','F32x2.LO_HI'):raise autocases.Unsupported('unknown mixed binary32 swizzle')
                value=lo|hi<<32;s.operand((name,),value,4+2*i,size=8)
            else:value=lo;autocase_extended.operand(s,name,value,4+2*i,32);selector='F32'
            np_=form.get(neg,'PP')=='NP' if neg else False
            if np_ and not half and (selector=='F32' or typ=='F32Imm'):
                raise autocases.Unsupported('NP broadcast lanes remain unresolved')
            inputs.append(mixed.ref_lanes(value,selector,form.get(name+'@absolute')=='set',form.get(name+'@negate')=='set',np_,half))
        if any(v is None for v in inputs):continue
        results=[];ftz=form.get('ftz')=='FTZ' or form.get('fmz')=='FTZ'
        for lane in (0,1):
            xs=[v[lane] for v in inputs]
            if exact:result=mixed.lane_result(op,xs,form['rnd'],ftz,dst)
            else:
                result=float.readable(op,xs,32)
                if result is not None and dst:
                    if dst=='F16' and float.conversion_underflow(result,32,16):result=None
                    else:result=fpformat.pack16(result,dst,'RN',False,False)
            if result is None:break
            results.append(result)
        if len(results)!=2:continue
        out=s.output(('Rd',),4 if j&1 else 2)
        s.add({out:results[0]|results[1]<<16} if dst else pair(out,results[0]|results[1]<<32),form,portable=True)
    return c
