"""Floating-point and predicate adapters; expectations use independent test references."""
import itertools
from sass.semantics.fp import softfloat
from tests.harness import autocase_profiles
from tests.harness.case import source, put
from tests.refs import integer
from tests.refs import float

FLOAT_OPS = {'FADD','FMUL','FFMA','DADD','DMUL','DFMA','FSEL','FMNMX','FMNMX3','FSET','FSETP','UFSETP','DSETP','FCHK','FRND','UFADD','UFMUL','UFFMA','UFSEL','UFRND'}
PREDICATE_OPS = {'PLOP3','UPLOP3','P2R','R2P','UP2UR'}
OPS = FLOAT_OPS | PREDICATE_OPS
FAMILIES = {op:'predicates' if op in PREDICATE_OPS else 'fp64' if op.startswith('D') else 'conversions' if op in ('FRND','UFRND') else 'fp32' for op in OPS}


def operand(s, name, value, reg, width):
    """FP64 immediates contain the high 32 bits of a double, unlike register pairs."""
    from tests.harness import autocases
    if name is None: raise autocases.Unsupported('unsupported floating source layout')
    kind = s.k.operand_types[name].type
    if kind == 'CX':
        s.operand((name,), value, reg, size=width//8)
        return value
    encoded = value >> 32 if width == 64 and kind == 'F64Imm' else value
    if not put(s.k,name,encoded,s.fields,s.registers,s.memory,reg,bank=(2,reg*8),imm=('F16Imm','F32Imm','F64Imm','UImm'),size=width//8):
        raise autocases.Unsupported('unsupported floating source type: '+kind)
    return (encoded << 32 if width == 64 else encoded) if kind == 'F64Imm' else value


def generate(c,k,form,profiles):
    from tests.harness import autocases
    expected_profiles = ['predicate_forms'] if k.mnemonic in PREDICATE_OPS else ['floating_forms','fchk_boundaries'] if k.mnemonic=='FCHK' else ['floating_forms']
    if profiles != expected_profiles: raise autocases.Unsupported('incompatible extended profiles')
    if k.mnemonic in PREDICATE_OPS: return predicates(c,k,form)
    reason = blocked_reason(k.mnemonic,form)
    if reason: raise autocases.Unsupported(reason)
    return floating(c,k,form)


def blocked_reason(op,form):
    """Cheap profile eligibility check shared with planning; no case/MD construction needed."""
    if op not in FLOAT_OPS: return None
    if op.removeprefix('U') not in ('FCHK','FRND','FSEL') and any('@' in n and not n.endswith('@not') and v != 'unset' for n,v in form.items()):
        return 'source-sign modifier corpus remains manual'
    if form.get('xorsign','noxorsign') != 'noxorsign': return 'XORSIGN remains a focused manual regression'
    if form.get('fmz','nofmz') not in ('nofmz','nofmz_hfma2','FTZ'): return 'FMZ zero/Inf corpus remains manual'
    return None


def floating(c,k,form):
    from tests.harness import autocases
    op=k.mnemonic.removeprefix('U')
    allowed={'rnd','ftz','fmz','sat','scale','nan','xorsign','fcomp','bop','test','fmt'}
    if any(n not in allowed and not n.endswith(('@not','@absolute','@negate')) for n in form):
        raise autocases.Unsupported('unhandled floating modifiers')
    width=64 if op.startswith('D') or form.get('fmt')=='F64' else 32
    if op=='FRND' and form.get('fmt') not in ('F32','F64'): raise autocases.Unsupported('unsupported round format')
    arithmetic=op in ('FADD','FMUL','FFMA','DADD','DMUL','DFMA')
    names=[source(k,'Ra','Sa','URa'),source(k,'Sb','Rb','URb'),source(k,'Sc','Rc','URc')]
    if op in ('FADD','DADD','DSETP'): names=[names[0],names[2]]
    elif op=='FRND': names=[source(k,'Sb','Rb','URb')]
    elif op not in ('FFMA','DFMA','FMNMX3'): names=names[:2]
    if not all(names): raise autocases.Unsupported('unsupported floating operand layout')
    allowed_comp=set(float.FCOMPARISONS)|{'F','T'}
    cmp=form.get('fcomp',form.get('test'))
    if op in ('FSET','FSETP','UFSETP','DSETP') and cmp not in allowed_comp:
        raise autocases.Unsupported('unresolved floating comparison')
    if any(n in k.operand_types for n in ('Pu','Pv')) and op in ('FMNMX','FMNMX3'):
        raise autocases.Unsupported('unresolved min/max predicate outputs')
    values=autocase_profiles.PROFILES['floating_forms'][width]
    vectors=[(a,values[(j+3)%len(values)],values[(j+5)%len(values)]) for j,a in enumerate(values)]
    if op=='FCHK':
        spec=autocase_profiles.PROFILES['fchk_boundaries']
        vectors += [(a,b,0) for a,b in (*autocase_profiles.fchk_pairs()[::spec['stride']],*spec['extra_pairs'])]
    for j,raw in enumerate(vectors):
        s=autocases.Inputs(c,k,form)
        xs=[]
        for n,v,r in zip(names,raw,(4,6,8)):
            value=operand(s,n,v,r,width)
            xs.append((float.conversion_source if op=='FRND' else float.source_modifiers)(value,width,form.get(n+'@absolute')=='set',form.get(n+'@negate')=='set'))
        p=source(k,'Pp','UPp')
        pred=j&1
        if p:
            s.predicate((p,),pred,0)
            pred ^= form.get(p+'@not')=='set'
        exact=c.fp=='exact'; ftz=exact and (form.get('ftz')=='FTZ' or form.get('fmz')=='FTZ')
        if arithmetic:
            operation={'ADD':'add','MUL':'mul','FMA':'fma'}[op[1:]]
            scale={'D8':-3,'D4':-2,'D2':-1,'M2':1,'M4':2,'M8':3,'noscale':0}.get(form.get('scale','noscale'))
            if scale is None: raise autocases.Unsupported('unknown multiply scale')
            sat=form.get('sat')=='SAT'
            if exact:
                result=softfloat.arithmetic(operation,xs,width,form['rnd'],ftz,sat,scale)
                if softfloat.unpack(result,width)[0]=='nan' or form['rnd']=='RN' and float.underflow_band(operation,xs,width,scale): continue
            else: result=float.readable(operation,xs,width,sat,scale)
            if result is None: continue
        elif op=='FRND':
            mode={'ROUND':'RN','TRUNC':'RZ','FLOOR':'RM','CEIL':'RP'}.get(form['rnd'])
            if mode is None: raise autocases.Unsupported('unknown integral rounding mode')
            result=float.frnd(xs[0],width,mode,form.get('ftz')=='FTZ')
        elif op=='FCHK':
            s.add({s.output(('Pu',),1):float.fchk_divide(*xs)},form,portable=True); continue
        elif op in ('FSETP','UFSETP','DSETP','FSET'):
            prepared=[float.flush(x,width) if ftz else x for x in xs]
            condition=float.fcompare(*(float.ordered(x,width) for x in prepared),cmp)
            yes=integer.combine(condition,pred,form['bop']); no=integer.combine(not condition,pred,form['bop'])
            if op!='FSET':
                s.add({s.output(('Pu','UPu'),0):yes,s.output(('Pv','UPv'),1):no},form,portable=True); continue
            result=0x3f800000 if yes else 0
        elif op=='FSEL': result=(float.flush(xs[0],32) if ftz else xs[0]) if pred else (float.flush(xs[1],32) if ftz else xs[1])
        elif op=='FMNMX':
            result=float.fmnmx(*xs,bool(pred),form.get('nan')=='NAN',ftz) if exact else float.fmnmx_readable(*xs,bool(pred))
        elif op=='FMNMX3':
            if ftz: xs=[float.flush(x,32) for x in xs]
            if exact and form.get('nan')=='NAN' and any(float.nan32(x) for x in xs): result=0x7fffffff
            else: result=float.fmnmx3_pick(float.fmnmx3_pick(xs[0],xs[1],bool(pred),exact),xs[2],bool(pred),exact)
        else: raise autocases.Unsupported('missing floating reference')
        rd=(4,6,8,2)[j%4]
        dest=s.output(('Rd','URd'),rd)
        expected={dest:result & 0xffffffff}
        if width==64: expected[dest.rstrip('0123456789')+str(rd+1)]=result>>32
        s.add(expected,form,portable=True)
    return c


def predicates(c,k,form):
    from tests.harness import autocases
    op=k.mnemonic
    if op in ('PLOP3','UPLOP3'):
        if any(not n.endswith('@not') for n in form): raise autocases.Unsupported('unhandled predicate LUT modifier')
        names=[source(k,*ns) for ns in (('Pp','UPp'),('Pq','UPq'),('Pr','UPr'))]
        if not all(names) or 'uimm8' not in k.operand_types or 'vimm8' not in k.operand_types:
            raise autocases.Unsupported('unresolved predicate LUT layout')
        for table in autocase_profiles.PROFILES['predicate_forms']['tables']:
            for xs in itertools.product((0,1),repeat=3):
                s=autocases.Inputs(c,k,form)
                for n,v,r in zip(names,xs,(0,1,2)): s.predicate((n,),v,r)
                effective=[v ^ (form.get(n+'@not')=='set') for n,v in zip(names,xs)]
                s.fields.update(uimm8=table,vimm8=table^255)
                a,b=integer.predicate_lut(table,table^255,*effective)
                s.add({s.output(('Pu','UPu'),0):a,s.output(('Pv','UPv'),1):b},form)
    else:
        selector='a_bsel' if op=='R2P' else 'insert'
        if set(form)!={selector}: raise autocases.Unsupported('unhandled predicate-transfer modifiers')
        byte=int(form[selector][1:])
        mask_name=source(k,'Sb','Rb','URb')
        for j,mask in enumerate(autocase_profiles.PROFILES['predicate_forms']['masks']):
            s=autocases.Inputs(c,k,form)
            s.operand((mask_name,),mask,6)
            packed=(0x55,0x2a,0,0x7f)[j%4]; base=(0xa5a5a5a5,0xffffffff,0x12345678)[j%3]
            uniform=op=='UP2UR'
            if op=='UP2UR': s.fields['UPR']='UPR'
            if op=='P2R': s.fields['Pr']='PR'
            s.operand(('Ra','URa'),base,4)
            prefix='UP' if uniform else 'P'
            s.registers.update({prefix+str(i):packed>>i&1 for i in range(7)})
            if op=='R2P':
                result=integer.r2p(packed,base,mask,byte)
                expected={prefix+str(i):result>>i&1 for i in range(7)}
            else: expected={s.output(('Rd','URd'),4 if j&1 else 2):integer.p2r(packed,base,mask,byte)}
            s.add(expected,form,portable=True)
    return c
