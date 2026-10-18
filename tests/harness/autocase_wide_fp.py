"""Binary32-pair arithmetic and ordinary F2FP pack form adapters."""
from sass.semantics.fp import softfloat
from tests.harness import autocase_profiles, autocase_extended
from tests.harness.case import source, pair
from tests.refs import float, fpformat

PAIR_OPS={'FADD2','FMUL2','FFMA2'}
PACK_OPS={'F2FP','UF2FP'}
OPS=PAIR_OPS | PACK_OPS
FAMILIES={op:'fp32x2' if op in PAIR_OPS else 'f2fp' for op in OPS}


def formats(form):
    if 'dfmt.sfmt' in form:return form['dfmt.sfmt'].split('.')
    return form.get('dstfmt'),form.get('srcfmt')


def blocked_reason(op,form):
    if op in PAIR_OPS:
        if any(n.endswith(('@absolute','@negate')) and v=='set' for n,v in form.items()):
            return 'packed F32 source-sign corpus remains manual'
        if form.get('fmz','nofmz') not in ('nofmz','nofmz_hfma2','FTZ'):
            return 'packed F32 FMZ numerical corpus remains manual'
    elif op in PACK_OPS:
        dst,src=formats(form)
        if src!='F32' or dst not in ('F16','BF16','E8M7','E6M9','TF32'):
            return 'low-precision, upconversion and scaled format corpora remain manual'
        if form.get('merge') not in (('PACK_B',) if dst=='TF32' else ('PACK_AB','MERGE_C')):
            return 'other packed conversion merge layouts remain manual'
        if set(form)-{'dstfmt','srcfmt','dfmt.sfmt','merge','rndMode','relu','satfinite','extract'}:
            return 'scaled and specialized packed conversion selectors remain manual'
    return None


def generate(c,k,form,profiles):
    from tests.harness import autocases
    expected=['binary32_pair_forms'] if k.mnemonic in PAIR_OPS else ['float_pack_forms']
    if profiles!=expected:raise autocases.Unsupported('incompatible wide floating profiles')
    reason=blocked_reason(k.mnemonic,form)
    if reason:raise autocases.Unsupported(reason)
    return pairs(c,k,form) if k.mnemonic in PAIR_OPS else pack(c,k,form)


def pairs(c,k,form):
    from tests.harness import autocases
    allowed={'rnd','ftz','fmz','iswzA','iswzB','iswzC','negA','negC'}
    if any(n not in allowed and not n.endswith(('@absolute','@negate')) for n in form):
        raise autocases.Unsupported('unhandled binary32 pair modifiers')
    if any(v not in ('F32x2.HI_LO','F32x2.LO_HI','F32') for n,v in form.items() if n.startswith('iswz')):
        raise autocases.Unsupported('unsupported binary32 lane selector')
    op={'FADD2':'add','FMUL2':'mul','FFMA2':'fma'}[k.mnemonic]
    names=[source(k,'Ra'),source(k,'Sb','Rb','URb'),source(k,'Sc','Rc','URc')]
    swizzles=['iswzA','iswzB','iswzC']; negs=['negA',None,'negC']
    if k.mnemonic=='FADD2':names=[names[0],names[2]];swizzles=['iswzA','iswzC'];negs=['negA','negC']
    elif k.mnemonic=='FMUL2':names=names[:2];swizzles=swizzles[:2];negs=negs[:2]
    if not all(names):raise autocases.Unsupported('unsupported binary32 pair source layout')
    values=autocase_profiles.PROFILES['binary32_pair_forms']['words']
    for j,a in enumerate(values):
        s=autocases.Inputs(c,k,form); inputs=[]
        for index,(n,sw,np) in enumerate(zip(names,swizzles,negs)):
            lo=values[(j+3*index)%len(values)];hi=values[(j+3*index+1)%len(values)]
            wide=sw in form and form[sw]!='F32' and k.operand_types[n].type!='F32Imm'
            if wide:
                s.operand((n,),lo | hi<<32,4+index*2,size=8)
                lanes=[lo,hi]
                if form[sw]=='F32x2.LO_HI':lanes.reverse()
            else:
                autocase_extended.operand(s,n,lo,4+index*2,32);lanes=[lo,lo]
            if np and form.get(np)=='NP' and k.operand_types[n].type=='F32Imm':
                raise autocases.Unsupported('NP immediate lanes remain unmodelled')
            if np:
                if form.get(np,'PP') not in ('PP','NP'):raise autocases.Unsupported('unknown lane sign selector')
                if form.get(np)=='NP':lanes[1]^=1<<31
            inputs.append(lanes)
        exact=c.fp=='exact';rnd=form['rnd'];ftz=form.get('ftz')=='FTZ' or form.get('fmz')=='FTZ'
        results=[]
        for lane in (0,1):
            xs=[v[lane] for v in inputs]
            if exact:
                result=softfloat.arithmetic(op,xs,32,rnd,ftz)
                if softfloat.unpack(result,32)[0]=='nan' or rnd=='RN' and float.underflow_band(op,xs,32):break
            else:result=float.readable(op,xs,32)
            if result is None:break
            results.append(result)
        if len(results)!=2:continue
        dest=s.output(('Rd',),4 if j&1 else 2)
        s.add(pair(dest,results[0] | results[1]<<32),form,portable=True)
    return c


def pack(c,k,form):
    from tests.harness import autocases
    dst,src=formats(form);dst='BF16' if dst=='E8M7' else dst
    rnd=form.get('rndMode','RN')
    if rnd not in ('RN','RZ'):raise autocases.Unsupported('unsupported ordinary pack rounding')
    values=autocase_profiles.PROFILES['float_pack_forms']['words']
    merge=form['merge'];relu=form.get('relu')=='RELU';sat=form.get('satfinite')=='SATFINITE'
    for j,a in enumerate(values):
        b=values[(j+3)%len(values)];base=(0xa53c91e7+j*0x10203)&0xffffffff
        s=autocases.Inputs(c,k,form)
        if merge=='PACK_AB':
            names=[source(k,'Ra','URa'),source(k,'Sb','Rb','URb')]
            for n,v,r in zip(names,(a,b),(4,6)):autocase_extended.operand(s,n,v,r,32)
            result=fpformat.pack16(a,dst,rnd,sat,relu)<<16 | fpformat.pack16(b,dst,rnd,sat,relu)
        else:
            n=source(k,'Sb','Rb','URb');autocase_extended.operand(s,n,b,4,32)
            if merge=='MERGE_C':
                name=source(k,'Sc','Rc','URc')
                if k.operand_types[name].type=='SImm':s.operand((name,),base,6)
                else:autocase_extended.operand(s,name,base,6,32)
                extract=form.get('extract','H0')
                if extract not in ('H0','H1'):raise autocases.Unsupported('unsupported preserved half selector')
                result=((base>>(16 if extract=='H1' else 0))&0xffff)<<16 | fpformat.pack16(b,dst,rnd,sat,relu)
            else:
                result=fpformat.tf32(b,rnd=='RN')
                if relu:result=0x7fffffff if result&0x7f800000==0x7f800000 and result&0x7fffff else 0 if result&0x80000000 else result
        dest=s.output(('Rd','URd'),4 if j&1 else 2)
        s.add({dest:result},form,portable=True)
    return c
