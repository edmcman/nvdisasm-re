"""Packed conversion and half-lane adapters, backed by independent test references."""
from tests.harness import autocase_profiles, autocase_extended, autocase_half_profiles
from tests.harness.case import source
from tests.refs import float, integer

PACK_OPS={'F2IP','I2IP'}
HALF_OPS={'HADD2','HMUL2','HFMA2','HFMA2.MMA','HMNMX2','HSET2','HSETP2'}
FMA_OPS={'HFMA2','HFMA2.MMA'}
OPS=PACK_OPS | HALF_OPS
FAMILIES={op:'conversions' if op in PACK_OPS else 'fp16' for op in OPS}
SWIZZLES={'H1_H0','H0_H0','H1_H1'}


def constant_move(op, form, cls):
    return (op in FMA_OPS and '__RRI' in cls
            and form.get('Ra@negate') == 'set' and form.get('Rb@negate', 'unset') == 'unset'
            and form.get('ofmt', 'F16_V2') == 'F16_V2'
            and form.get('fmz', 'nofmz') in ('nofmz', 'nofmz_hfma2')
            and form.get('sat', form.get('satrelu', 'nosat')) == 'nosat'
            and not any(n.endswith('@absolute') and v == 'set' for n, v in form.items())
            and all(v == 'H1_H0' for n, v in form.items() if n.startswith('iswz')))


def blocked_reason(op,form,cls=''):
    if op not in HALF_OPS:return None
    if constant_move(op, form, cls) or op == 'HADD2' and form.get('ofmt') == 'F32':
        return None
    mixed = any(n.startswith('iswz') and v == 'F32' for n,v in form.items())
    if (any(n.endswith(('@absolute','@negate')) and v=='set' for n,v in form.items())
            and op not in ('HADD2','HMUL2',*FMA_OPS)
            and not (mixed and op in ('HSET2','HSETP2'))):
        return 'packed source-sign numerical corpus remains manual'
    allowed = SWIZZLES | ({'F32'} if op in ('HADD2','HMUL2','HFMA2','HSET2','HSETP2') else set())
    if any(n.startswith('iswz') and v not in allowed for n,v in form.items()):
        return 'mixed-precision and sign-swizzle lane corpus remains manual'
    if mixed and op in ('HMUL2','HFMA2') and form.get('fmz', 'nofmz') in ('FTZ', 'FMZ'):
        return 'mixed F32 input with FTZ or FMZ remains unresolved'
    if form.get('xorsign','noxorsign')!='noxorsign' or 'isA' in form:
        return 'min/max XORSIGN and source-predicate outputs remain manual'
    if op in ('HADD2','HMUL2',*FMA_OPS) and form.get('ofmt','F16_V2')!='F16_V2':
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
    elif s.k.operand_types[name].type == 'F16Imm':
        s.fields[name] = value & 0xffff
        return value & 0xffff
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
    if constant_move(k.mnemonic, form, k.name):
        return literal_move(c, k, form)
    if k.mnemonic == 'HADD2' and form.get('ofmt') == 'F32':
        return widening(c, k, form)
    reason=blocked_reason(k.mnemonic,form,k.name)
    if reason:raise autocases.Unsupported(reason)
    return half(c,k,form)


def literal_move(c, k, form):
    from tests.harness import autocases
    if not {'Ra', 'Rb', 'Sc', 'Sc1'} <= k.operand_types.keys():
        raise autocases.Unsupported('literal half move needs two zero registers and two immediate lanes')
    for word in autocase_profiles.PROFILES['half_forms']['F16']:
        s = autocases.Inputs(c, k, form)
        s.fields.update(Ra='RZ', Rb='RZ', Sc=word >> 16, Sc1=word & 0xffff)
        # Independently rounded fused -0*+0+literal, including canonical NaNs.
        result = float.half2('fma', [(0x8000, 0x8000), (0, 0), (word & 0xffff, word >> 16)])
        s.add({s.output(('Rd',), 8): result}, form, portable=True)
    return c


def widening(c, k, form):
    from tests.harness import autocases
    if c.arch == 'SM75':
        return widening_sm75(c, k, form)
    names = [source(k, 'Ra'), source(k, 'Rc', 'Sc', 'URc')]
    zero = next((n for n in names if n in k.operand_types
                 and k.operand_types[n].type in ('Register', 'ZeroRegister')
                 and form.get(n+'@negate') == 'set'
                 and (c.arch == 'SM80' or form.get(n+'@absolute', 'unset') == 'unset')), None)
    if zero is None:
        raise autocases.Unsupported('F32 half widening requires an explicit negative zero register')
    other = next(n for n in names if n != zero)
    swizzle = 'iswzA' if other == 'Ra' else source(k, 'iswzB_as_C', 'iswzB')
    selector = form.get(swizzle, 'H1_H0')
    if selector not in SWIZZLES | {'F32'}:
        raise autocases.Unsupported('unresolved widening source swizzle')
    if form.get('sat', 'nosat') not in ('nosat', 'SAT'):
        raise autocases.Unsupported('unresolved widening saturation')
    spec = autocase_profiles.PROFILES['half_forms']
    values = (autocase_profiles.PROFILES['half_f32_sources']['floats'] if selector == 'F32'
              else tuple(dict.fromkeys(spec['F16'] + spec['widening'])))
    for value in values:
        s = autocases.Inputs(c, k, form)
        s.fields[zero] = 'RZ'
        if selector == 'F32':
            raw = value & (0x7fffffff if form.get(other+'@absolute') == 'set' else 0xffffffff)
            raw ^= 0x80000000 if form.get(other+'@negate') == 'set' else 0
            lane = float.f2f(raw, 32, 16, 'RZ')
        else:
            raw = value & (0x7fff7fff if form.get(other+'@absolute') == 'set' else 0xffffffff)
            raw ^= 0x80008000 if form.get(other+'@negate') == 'set' else 0
            lane = lanes(raw, selector)[0]
        operand(s, other, value, 4)
        result = float.widen_half(float.rounded('add', [0x8000, lane], 16,
                                  ftz=c.fp == 'exact' and form.get('ftz') == 'FTZ',
                                  sat=form.get('sat') == 'SAT'))
        if result is not None:
            s.add({s.output(('Rd',), 8): result}, form, portable=True)
    return c


def widening_sm75(c, k, form):
    """Both half inputs may be nonzero in SM75's F32-result HADD2."""
    from tests.harness import autocases
    names = [source(k, 'Ra'), source(k, 'Rc', 'Sc', 'URc')]
    if not all(names): raise autocases.Unsupported('unsupported widening source layout')
    swizzles = ['iswzA', source(k, 'iswzB_as_C', 'iswzB')]
    selectors = [form.get(sw, 'H1_H0') for sw in swizzles]
    if any(sw not in SWIZZLES | {'F32'} for sw in selectors):
        raise autocases.Unsupported('unresolved widening source swizzle')
    if form.get('sat', 'nosat') not in ('nosat', 'SAT'):
        raise autocases.Unsupported('unresolved widening saturation')
    zero = next((n for n in names if getattr(c, 'operand_constraints', {}).get(n) == 'RZ'), None)
    if form.get('ftz') == 'FTZ' and 'F32' in selectors and zero is None:
        raise autocases.Unsupported('F32 widening with mixed input and FTZ remains unresolved')

    spec = autocase_profiles.PROFILES['half_forms']
    values = tuple(dict.fromkeys(spec['F16'] + spec['widening']))
    vectors = [(v, values[(j + 3) % len(values)]) for j, v in enumerate(values)]
    vectors += [(0x3c003c00, 0x10001000), (0x10001000, 0x3c003c00)]
    mixed = [sw == 'F32' for sw in selectors]
    if any(mixed):
        mixed_spec = autocase_profiles.PROFILES['half_f32_sources']
        vectors = [tuple(mixed_spec['floats'][(j + 3 * n) % len(mixed_spec['floats'])]
                         if mixed[n] else v for n, v in enumerate(pair))
                   for j, pair in enumerate(vectors)]
        vectors += [tuple(x if is_float else half for is_float in mixed)
                    for x, half in mixed_spec['pairs']]
        if all(mixed): vectors += list(mixed_spec['float_pairs'])

    for j, raw in enumerate(vectors):
        s = autocases.Inputs(c, k, form)
        inputs = []
        for n, value, reg, selector in zip(names, raw, (4, 6), selectors):
            if n == zero:
                s.fields[n] = 'RZ'
                value = 0
            else:
                value = operand(s, n, value, reg)
            inputs.append(float.half_source(value, selector,
                                             form.get(n+'@absolute') == 'set',
                                             form.get(n+'@negate') == 'set')[0])
        sat = form.get('sat') == 'SAT'
        ftz = c.fp == 'exact' and form.get('ftz') == 'FTZ'
        if c.fp == 'exact':
            if float.underflow_band('add', inputs, 16): continue
            result = float.rounded('add', inputs, 16, ftz=ftz, sat=sat)
        else:
            result = float.readable('add', inputs, 16, sat=sat)
        result = None if result is None else float.widen_half(result)
        if result is not None:
            s.add({s.output(('Rd',), (4, 6, 2)[j % 3]): result}, form, portable=True)
    return c


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
    elif op not in FMA_OPS:names=names[:2]
    if not all(names):raise autocases.Unsupported('unsupported half source layout')
    swizzles=['iswzA']
    if op in FMA_OPS:swizzles += [next((n for n in ('iswzB','iswzC_as_B') if n in form),None),next((n for n in ('iswzC','iswzB_as_C') if n in form),None)]
    else:swizzles += [next((n for n in ('iswzB_as_C','iswzB') if n in form),None)]
    values=autocase_profiles.PROFILES['half_forms'][fmt]
    mixed = [form.get(sw) == 'F32' for sw in swizzles]
    samples=autocase_half_profiles.select(op,form,c.fp) if op in ('HSET2','HSETP2') and not any(mixed) else ((j,j&1) for j in range(len(values)))
    vectors=[((values[j],values[(j+3)%len(values)],values[(j+5)%len(values)]),p) for j,p in samples]
    if op in ('HADD2','HMUL2','HFMA2') and fmt=='F16':
        vectors += [(v,0) for v in autocase_profiles.half_nan_vectors(len(names))]
    vectors=[(raw,p,None) for raw,p in vectors]
    if any(mixed):
        spec = autocase_profiles.PROFILES['half_f32_sources']
        vectors = [(tuple(spec['floats'][(j+3*n)%len(spec['floats'])] if mixed[n] else v
                          for n,v in enumerate(raw[:len(names)])),p,None)
                   for j,(raw,p,_) in enumerate(vectors)]
        vectors += [(tuple(x if is_float else half for is_float in mixed),p,None)
                    for x,half in spec['pairs'] for p in (0,1)]
        if all(mixed):
            vectors += [(tuple(pair[n % len(pair)] for n in range(len(mixed))),p,None)
                        for pair in spec['float_pairs'] for p in (0,1)]
        if op in ('HMUL2', 'HFMA2') and mixed.count(True) == 1:
            index = mixed.index(True)
            # Neutral operands isolate input rounding even when a PTX multiply/add contracts.
            neutral = [0x3c003c00, 0x3c003c00] if op == 'HMUL2' else [0x3c003c00, 0x3c003c00, 0]
            if op == 'HFMA2' and index == 2: neutral[0] = 0
            for value in (0x3f801800, 0xbf801800, 0x7fc12345, 0xffc54321, 0x7f812345, 0xff812345):
                raw = list(neutral)
                raw[index] = value
                vectors.append((tuple(raw), 0, None))
            for other in range(len(names)):
                if other == index: continue
                for half_nan in (0x7e55, 0xfe23, 0x7d01, 0xfc17):
                    raw = list(neutral)
                    raw[index] = 0x3f800000
                    raw[other] = half_nan | (half_nan << 16)
                    vectors.append((tuple(raw), 0, None))
            if op == 'HMUL2' or index < 2:
                raw = list(neutral)
                raw[index] = 0x7f800000
                raw[1 - index] = 0
                vectors.append((tuple(raw), 0, None))
            else:
                raw = list(neutral)
                raw[0], raw[1], raw[2] = 0, 0x7c007c00, 0x3f800000
                vectors.append((tuple(raw), 0, None))
        if op == 'HADD2' and mixed.count(True) == 1:
            other = names[mixed.index(False)]
            if (k.operand_types[other].type in ('Register','ZeroRegister')
                    and form.get(other+'@negate') == 'set'
                    and form.get(other+'@absolute','unset') == 'unset'):
                vectors += [(tuple(x if is_float else 0 for is_float in mixed),0,other)
                            for x in spec['floats']]
    for j,(raw,raw_predicate,zero) in enumerate(vectors):
        s=autocases.Inputs(c,k,form)
        pairs=[]
        for n,v,r,sw in zip(names,raw,(4,6,8),swizzles):
            value = operand(s,n,v,r)
            if n == zero: s.fields[n] = 'RZ'
            pairs.append(float.half_source(value,form.get(sw,'H1_H0'),
                                           form.get(n+'@absolute') == 'set',
                                           form.get(n+'@negate') == 'set'))
        packed=[lo | hi<<16 for lo,hi in pairs]
        p=raw_predicate; pred=source(k,'Pp')
        if pred:
            s.predicate((pred,),p,0);p ^= form.get(pred+'@not')=='set'
        ftz=form.get('ftz')=='FTZ'; exact=c.fp=='exact'
        if op in ('HADD2','HMUL2',*FMA_OPS):
            operation='fma' if op in FMA_OPS else {'HADD2':'add','HMUL2':'mul'}[op]
            # .RELU applies while Pp holds: a result with its sign set becomes +0 (SM89 GPU).
            relu=form.get('satrelu')=='RELU' and bool(p)
            sat=form.get('sat',form.get('satrelu','nosat'))=='SAT'
            mode=form.get('fmz','nofmz')
            if mode not in ('nofmz','nofmz_hfma2','FMZ','FTZ'):
                raise autocases.Unsupported('unknown packed flush mode')
            results=[]
            for lane in (0,1):
                xs=[pair[lane] for pair in pairs]
                if exact:
                    if op=='HMUL2' and mode=='FMZ' and any(x&0x7c00==0 for x in xs):result=0
                    else:result=float.rounded(operation,xs,16,ftz=ftz or mode in ('FTZ','FMZ'),sat=sat)
                    if float.underflow_band(operation,xs,16):break
                else:result=float.readable(operation,xs,16,sat)
                if result is None:break
                results.append(0 if relu and result>>15 else result)
            if len(results)!=2:continue
            result=results[0] | results[1]<<16
        elif op=='HMNMX2':
            result=float.half_minmax(*packed,bool(p),form.get('nan')=='NAN',ftz,fmt=fmt)
        else:
            kw=dict(cmp=form['cmp'],bop=form['bop'],p=bool(p),ftz=exact and ftz,fmt=fmt)
            if op=='HSET2':result=float.hset2(*packed,bf=form['bval']=='BF',**kw)
            else:
                pu,pv=float.hsetp2(*packed,h_and=form['h_and']=='H_AND',**kw)
                s.setp_outputs(pu, pv, form);continue
        out=s.output(('Rd',),4 if j&1 else 2)
        s.add({out:result},form,portable=True)
    return c
