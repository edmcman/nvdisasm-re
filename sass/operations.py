"""Class-specific native overrides and explicit instruction primitives.

Unsupported selector combinations decode to complete-input opaque operations.
This module does not equate PTX syntax with undocumented SASS variants.
"""
from dataclasses import dataclass

OPERATION_FAMILIES = set('IADD3 IMAD LOP3 SHF LEA IABS IMNMX ISETP SEL PRMT MOV S2R CS2R LDC FADD FMUL FFMA FSETP F2I I2F I2FP MUFU LDG STG LDS STS LDL STL BRA EXIT CALL RET BSSY BSYNC BAR SHFL VOTE ULDC NOP'.split())
ALIASES = dict(UIADD3='IADD3', UIMAD='IMAD', USHF='SHF', ULEA='LEA',
               UISETP='ISETP', ULOP3='LOP3', USEL='SEL', UMOV='MOV', VIADD='IADD')
OPERATION_FAMILIES.update(ALIASES)
OPERATION_FAMILIES.update(('IADD','FSEL','FMNMX'))

PRIMITIVES = set('FADD FMUL FFMA FSETP F2I I2F I2FP MUFU PRMT SHFL VOTE BAR BSSY BSYNC'.split())

@dataclass(frozen=True)
class Override:
    family: str
    implementation: str

REGISTRY = {op: Override(op,'primitive' if op in PRIMITIVES else 'native') for op in OPERATION_FAMILIES}


def constrain_banks(b):
    from sass.gen_sleigh import ENUM_FILE,REG_FILES
    for name,value in {**b.values,**b.outputs}.items():
        atom=b.k.operand_types.get(name);file=ENUM_FILE.get(atom.type) if atom else None
        if value.kind=='register' and file in ('R','UR'):
            span=value.size//REG_FILES[file][1];zero=REG_FILES[file][3]
            if span>1:b.allow(name,lambda v,z=zero,s=span:v==z or v<max(0,z-s+1))


def dispatch(b,fallback='opaque'):
    import re
    from sass.gen_sleigh import pattern
    from sass.semantic import prune_unused_values
    native=' '.join(prune_unused_values(b.body))
    names=set(re.findall(r'\b\w+\b',native))
    refs=[r for r in b.refs if r[1] in names]
    fallback=' '.join(b.call(fallback))
    fallback_names=set(re.findall(r'\b\w+\b',fallback))
    fallback_refs=[r for r in b.refs if r[1] in fallback_names]
    constructors=[]
    if b.native_supported and pattern(b.native_constraints):
        # Share arithmetic bodies independently of each class's opaque ABI.
        calculation=b.g.subtable('native',[('""',pattern([],refs),'',native)])
        constructors.append(('""',pattern(b.native_constraints,[('sub',calculation)]),'',f'build {calculation};'))
    if not constructors or b.native_constraints:
        constructors.append(('""',pattern([],fallback_refs),'',fallback))
    table=b.g.subtable('dispatch',constructors)
    b.body=[f'build {table};'];b.refs=[('sub',table)]


def emit(b):
    op=b.k.mnemonic
    if op not in REGISTRY:return 'opaque','outside implemented operation families'
    fn=globals().get('emit_'+ALIASES.get(op,op))
    if op in PRIMITIVES and not (fn and fn(b,check=True)):
        constrain_banks(b)
        b.body+=b.call('prim')
        dispatch(b)
        return 'primitive','runtime validates selectors; unsupported combinations raise'
    if fn is None:return 'opaque','class variant not yet described by a native override'
    if not fn(b,check=True):return 'opaque',getattr(b,'unsupported_reason','unsupported class shape')
    constrain_banks(b)
    for name in b.values:
        if '@' in name and name.split('@')[1] not in ('absolute','negate','invert','not'):
            b.allow(name,lambda v:v==0)
    initial_body=list(b.body)
    fn(b,check=False)
    if op in PRIMITIVES:
        dispatch(b,'prim')
        return 'native','decode-time selector guards fall back to runtime primitives'
    if not b.native_supported:
        b.body=initial_body
        return 'opaque','class selectors have no supported native combination'
    dispatch(b)
    return 'native','decode-time selector guards retain complete opaque fallbacks'


def require(b,name,labels):
    if name not in b.values:return
    atom=b.k.operand_types.get(name)
    if not atom or atom.type not in b.g.arch.enums:return
    labels=[l for l in labels if l is not None]
    b.selector_guards[name]=list(labels)
    vals={b.g.arch.enums[atom.type][x] for x in labels if x in b.g.arch.enums[atom.type]}
    if not vals:b.reject();return
    b.allow(name,lambda v:v in vals)


def default(b,name):
    """The md default label of a selector, whose spelling varies by architecture."""
    atom=b.k.operand_types.get(name)
    return atom.default if atom else None


def operands(b,*groups):
    # Keep original md names so selector constraints and attribute lookup use the
    # actual encoding, including mixed ordinary/uniform register classes.
    return [next((n for n in group.split('/')+['U'+n for n in group.split('/')]
                  if n in b.values),None) for group in groups]


def output(b,name='Rd'):
    return b.outputs.get(name,b.outputs.get('U'+name))


def emit_MOV(b,check=False):
    src=operands(b,'Sb/Sa/Ra/Rb/URa/URb')[0]
    if check:return src is not None and output(b) is not None
    if 'PixMaskU04' in b.values:b.allow('PixMaskU04',lambda v:v==15)
    dst=output(b);b.body.append(f'{dst.symbol} = {b.cast(src,dst.size)};')

def emit_NOP(b,check=False):
    return not b.outputs if check else None

def emit_ULDC(b,check=False):
    if check:return 'URd' in b.outputs and all(n in b.values for n in ('Sa_bank','Sa_addr','sz')) and b.k.operand_types['Sa'].type=='C'
    bank=b.values['Sa_bank'].symbol;offset=b.values['Sa_addr'].symbol
    addr=b.expression(8,f'(zext({bank}) << 32) | zext({offset})')
    result=b.outputs['URd'];en=b.g.arch.enums[b.k.operand_types['sz'].type];cases={}
    for label,width,signed in [('U8',1,False),('S8',1,True),('U16',2,False),('S16',2,True),('32',4,False),('64',8,False)]:
        if label not in en or width>result.size:continue
        expr='v' if width==result.size else f'{"sext" if signed else "zext"}(v)'
        cases[(en[label],)]=f'local v:{width} = *[cbank]:{width} {addr.symbol}; local t:{result.size} = {expr}; export t;'
    b.allow('sz',lambda v:(v,) in cases)
    table=b.select_code(('sz',),cases);b.body.append(f'{result.symbol} = {table};')


def emit_IABS(b,check=False):
    src=operands(b,'Sb/Rb/Ra')[0]
    if check:return src is not None and output(b) is not None and output(b).size==4
    v=b.flag(src);t=b.local(4,v)
    b.body += [f'if ({t.symbol} s>= 0) goto <nonnegative>;',f'{t.symbol} = -{t.symbol};','<nonnegative>',f'{output(b).symbol} = {t.symbol};']


def emit_SEL(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and output(b) is not None
    av,cv,pv=b.flag(a),b.flag(c),b.flag(p)
    t=b.local(output(b).size,av)
    b.body += [f'if ({pv} != 0) goto <selected>;',f'{t.symbol} = {cv};','<selected>',f'{output(b).symbol} = {t.symbol};']


def emit_IMAD(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:
        if not (all((a,c,d)) and output(b) is not None and all(b.values[n].size<=8 for n in (a,c,d))):return False
        mode=b.k.operand_types.get('wide')
        labels=b.g.arch.enums.get(mode.type,{}) if mode else {}
        if set(labels)&{'HI','WIDE'} and b.k.operand_types[d].type in ('C','CX') and b.values[d].size<8:
            b.unsupported_reason='32-bit constant addend extension for HI/WIDE is unverified'
            return False
        return True
    if 'X' in b.values:b.reject()
    if 'x' in b.values:require(b,'x',['nox','NOX']) # unresolved carry modes stay opaque
    if any(n in b.values for n in ('Pp','Pq','UPp','UPq')):b.reject()
    require(b,'fmt',['U32','S32'])
    for name in b.outputs:
        if name not in ('Rd','URd'):
            b.allow(name,lambda v:v==7)
    aa,bb=b.flag(a),b.flag(c)
    wide=b.k.operand_types.get('wide');labels=b.g.arch.enums.get(wide.type,{}) if wide else {}
    mode=next(iter(labels),'LO');rd=output(b)
    if mode=='LO':
        # Low-word multiplication and addition are independent of signedness.
        product=b.local(4,f'{aa} * {bb}')
        addend=b.flag(d)
        if b.values[d].size>4:addend=b.local(4,f'{addend}:4').symbol
        result=b.local(4,f'{product.symbol} + {addend}')
    else:
        fmt=b.k.operand_types.get('fmt');formats=b.g.arch.enums.get(fmt.type,{}) if fmt else {}
        x=b.select('fmt',{value:f'{"sext" if label=="S32" else "zext"}({aa})'
                          for label,value in formats.items()},8) if formats else b.local(8,f'zext({aa})').symbol
        y=b.select('fmt',{value:f'{"sext" if label=="S32" else "zext"}({bb})'
                          for label,value in formats.items()},8) if formats else b.local(8,f'zext({bb})').symbol
        product=b.local(8,f'{x} * {y}')
        rc=b.flag(d)
        if b.values[d].size<8:rc=b.local(8,f'zext({rc})').symbol
        result=b.local(8,f'{product.symbol} + {rc}')
        if mode=='HI':result=b.local(8,f'{result.symbol} >> 32')
    for name,out in b.outputs.items():
        if name in ('Rd','URd'):b.body.append(f'{out.symbol} = {result.symbol}{":4" if result.size>out.size else ""};')


def emit_IADD3(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,c,d)) and output(b) is not None and all(b.values[n].size==4 for n in (a,c,d))
    # .X's carry-input convention is deliberately not guessed.
    if 'X' in b.values:b.reject()
    if any(n in b.values for n in ('Pp','Pq','UPp','UPq')):b.reject()
    aa,bb,cc=(b.flag(n) for n in (a,c,d))
    first=b.local(4,f'{aa} + {bb}')
    total=b.local(4,f'{first.symbol} + {cc}')
    # Predicate/carry conventions require independent SASS evidence. Only PT
    # destinations are handled here; other cases go through the complete fallback.
    for name,out in b.outputs.items():
        if name not in ('Rd','URd'):
            b.allow(name,lambda v:v==7)
    b.body.append(f'{output(b).symbol} = {total.symbol};')


def emit_IADD(b,check=False):
    a,c=operands(b,'Ra/Sa','Rb/Sb')
    if check:return all((a,c)) and output(b) is not None and output(b).size==4 and all(b.values[n].size==4 for n in (a,c))
    require(b,'fmt',['32','_32','U32','S32']);require(b,'isat',[default(b,'isat')])
    if any(n in b.values for n in ('X','Pp','UPp')):b.reject()
    for name in b.outputs:
        if name not in ('Rd','URd'):b.allow(name,lambda v:v==7)
    b.body.append(f'{output(b).symbol} = {b.flag(a)} + {b.flag(c)};')


def emit_LOP3(b,check=False):
    a,c,d,p=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc','Pp');lut=operands(b,'imm8')[0]
    if check:return all((a,c,d,lut)) and output(b) is not None
    require(b,'pop',['POR','PAND'])
    av,bv,cv=(b.flag(n) for n in (a,c,d))
    def logic(bits,inputs):
        mask=(1 << (1 << len(inputs)))-1
        if bits==0:return '0:4'
        if bits==mask:return '0xffffffff:4'
        v,*tail=inputs;half=1 << len(tail);low=bits & ((1<<half)-1);high=bits>>half
        if low==high:return logic(low,tail)
        lo,hi=logic(low,tail),logic(high,tail);full=(1<<half)-1
        if low==0 and high==full:return v
        if low==full and high==0:return f'~({v})'
        if high==(low^full):return f'({v}) ^ ({lo})'
        if low==0:return f'({v}) & ({hi})'
        if high==0:return f'~({v}) & ({lo})'
        if low==full:return f'~({v}) | ({hi})'
        if high==full:return f'({v}) | ({lo})'
        return f'(({v}) & ({hi})) | (~({v}) & ({lo}))'
    result=b.select(lut,{v:logic(v,(av,bv,cv)) for v in range(256)},4)
    rd=output(b).symbol
    predicate=next((name for name in b.outputs if name not in ('Rd','URd')),None)
    plain=f'{rd} = {result};'
    if predicate is None:
        b.body.append(plain);return
    pv=b.flag(p) if p else None
    prefix=f'local value:4 = {result}; local pred:1 = value != 0; '
    suffix=f'{b.outputs[predicate].symbol} = pred; {rd} = value;'
    if p and 'pop' in b.values:
        en=b.g.arch.enums[b.k.operand_types['pop'].type]
        table=b.select_code(('pop',),{(en[label],):prefix+f'pred = pred {oper} {pv}; '+suffix
                                    for label,oper in [('POR','|'),('PAND','&')] if label in en})
        full=f'build {table};'
    else:full=prefix+suffix
    from sass.gen_sleigh import pattern
    import re
    constraints=b.g.values_where(b.k,predicate,lambda v:v==7)
    if constraints is None:
        b.body.append(full);return
    constructors=[]
    for clauses,code in [(constraints,plain),([],full)]:
        names=set(re.findall(r'\b\w+\b',code))
        refs=[r for r in b.refs if r[1] in names]
        constructors.append(('""',pattern(clauses,refs),'',code))
    table=b.g.subtable('predicate',constructors)
    b.refs.append(('sub',table));b.body.append(f'build {table};')


def emit_PRMT(b,check=False):
    a,selector,c=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,selector,c)) and output(b) is not None and output(b).size==4 and all(b.values[n].size==4 for n in (a,selector,c))
    modes=enum_cases(b,'pmode',['IDX','F4E','B4E','RC8','RC16','ECL','ECR'])
    data=b.expression(8,f'zext({b.flag(a)}) | (zext({b.flag(c)}) << 32)')
    sel=b.flag(selector);cases={}
    for mode,value in modes.items():
        code='local r:4 = 0; '
        for i in range(4):
            pick={'IDX':f'({sel} >> {4*i}) & 15', 'F4E':f'({sel} & 3) + {i}',
                  'B4E':f'(({sel} & 3) - {i}) & 7', 'RC8':f'{sel} & 3',
                  'RC16':f'(({sel} & 1) * 2) + {i&1}',
                  'ECL':f'{sel} & 3', 'ECR':f'{sel} & 3'}[mode]
            code+=f'local p{i}:4 = {pick}; '
            if mode in ('ECL','ECR'):
                oper='>=' if mode=='ECL' else '<='
                code+=f'if (p{i} {oper} {i}) goto <pick{i}>; p{i} = {i}; <pick{i}> '
            code+=f'local chunk{i}:8 = {data.symbol} >> ((p{i} & 7) * 8); local byte{i}:4 = chunk{i}:4 & 255; '
            if mode=='IDX':
                code+=f'if ((p{i} & 8) == 0) goto <signbyte{i}>; byte{i} = 255 * zext((byte{i} & 128) != 0); <signbyte{i}> '
            code+=f'r = r | (byte{i} << {8*i}); '
        cases[(value,)]=code+'export r;'
    result=b.select_code(('pmode',),cases)
    b.body.append(f'{output(b).symbol} = {result};')


def emit_SHF(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,c,d)) and output(b) is not None
    require(b,'fmt',['U32','S32','U64','S64']);require(b,'dir',['L','R']);require(b,'cw',['C','W']);require(b,'hilo',['LO','HI'])
    av,shift,cv=(b.flag(n) for n in (a,c,d))
    enums={n:b.g.arch.enums[b.k.operand_types[n].type] for n in ('fmt','dir','cw','hilo')}
    counts={}
    for fmt in ('U32','S32','U64','S64'):
        limit=32 if fmt.endswith('32') else 64
        for mode in ('C','W'):
            if fmt not in enums['fmt'] or mode not in enums['cw']:continue
            if mode=='W':code=f'local t:4 = {shift} & {limit-1}; export t;'
            else:code=f'local t:4 = {shift}; if (t <= {limit}) goto <clamped>; t = {limit}; <clamped> export t;'
            counts[(enums['fmt'][fmt],enums['cw'][mode])]=code
    count=b.select_code(('fmt','cw'),counts)
    base=b.expression(8,f'(zext({cv}) << 32) | zext({av})')
    cases={}
    for fmt in ('U32','S32','U64','S64'):
        for direction in ('L','R'):
            for half in ('LO','HI'):
                if any(label not in enums[name] for name,label in [('fmt',fmt),('dir',direction),('hilo',half)]):continue
                operator='<<' if direction=='L' else 's>>' if fmt.startswith('S') else '>>'
                expr=f'{base.symbol} {operator} {count}'
                code=f'local t:8 = {expr}; '
                if half=='HI':code+='t = t >> 32; '
                code+='export t;'
                cases[(enums['fmt'][fmt],enums['dir'][direction],enums['hilo'][half])]=code
    result=b.select_code(('fmt','dir','hilo'),cases)
    b.body.append(f'{output(b).symbol} = {result}:4;')


def comparison(b,a,c,name='icmp'):
    enums=b.g.arch.enums[b.k.operand_types[name].type]
    require(b,name,['F','LT','EQ','LE','GT','NE','GE','T'])
    fmt=b.k.operand_types.get('fmt');formats=b.g.arch.enums.get(fmt.type,{}) if fmt else {None:None}
    names=(name,'fmt') if fmt else (name,)
    cases={}
    for label,oper in [('F',None),('LT','<'),('EQ','=='),('LE','<='),('GT','>'),('NE','!='),('GE','>='),('T',None)]:
        if label not in enums:continue
        for kind,value in formats.items():
            expr=str(1 if label=='T' else 0) if oper is None else f'{a} {"s" if kind=="S32" and oper not in ("==","!=") else ""}{oper} {c}'
            key=(enums[label],value) if fmt else (enums[label],)
            cases[key]=f'local t:1 = {expr}; export t;'
    return b.select_code(names,cases)


def combine(b,cond,p):
    require(b,'bop',['AND','OR','XOR']);en=b.g.arch.enums[b.k.operand_types['bop'].type]
    return b.select('bop',{en[label]:f'{cond} {oper} {p}' for label,oper in [('AND','&'),('OR','|'),('XOR','^')] if label in en},1)


# Native fp32 p-code covers round-to-nearest with .FTZ/.SAT; directed rounding,
# .FMZ, and other selectors fall back to sass_prim_*.
FLUSH='{0} & (0x80000000 | (0x7fffffff * zext(({0} & 0x7f800000) != 0)))'
SATURATE=('if (!nan(r) && (0:4 f< r)) goto <positive>; r = 0; goto <saturated>; '
          '<positive> if (r f<= 0x3f800000:4) goto <saturated>; r = 0x3f800000; <saturated> ')
SCALES={'noscale':0,'D2':-1,'D4':-2,'D8':-3,'M2':1,'M4':2,'M8':3}

def enum_cases(b,name,labels):
    """{label: value} for the labels of selector `name` this class can encode; {None: None} if absent."""
    if name not in b.values:return {None:None}
    en=b.g.arch.enums[b.k.operand_types[name].type]
    require(b,name,[l for l in labels if l in en])
    return {l:en[l] for l in labels if l in en}

def float_inputs(b,names):
    """Operand symbols after |x|/-x flags and, under .FTZ, denormal flushing."""
    mode='fmz' if 'fmz' in b.values else 'ftz'
    flush=enum_cases(b,mode,['FTZ','noftz','nofmz','nofmz_hfma2'])
    out=[]
    for name in names:
        v=b.flag(name,True)
        out.append(v if flush=={None:None} else
                   b.select(mode,{value:FLUSH.format(v) if label=='FTZ' else v for label,value in flush.items()},4))
    return mode,flush,out

def float_result(b,mode,flush,core,extra=None):
    """Select the body computing r per (.FTZ, .SAT[, extra]) and assign it to Rd."""
    require(b,'rnd',['RN'])
    sat=enum_cases(b,'sat',['SAT','nosat'])
    extra=extra or (None,{None:''})
    names=tuple(n for n,c in ((mode,flush),('sat',sat),(extra[0],extra[1])) if n and c!={None:None})
    cases={}
    for fl,fv in flush.items():
        for sl,sv in sat.items():
            for el,ev in extra[1].items():
                code=core(el)+(f' r = {FLUSH.format("r")};' if fl=='FTZ' else '')+(' '+SATURATE if sl=='SAT' else ' ')+'export r;'
                key=tuple(v for n,v in ((mode,fv),('sat',sv),(extra[0],ev)) if n in names)
                cases[key]=code
    table=b.select_code(names,cases)
    b.body.append(f'{output(b).symbol} = {table};')

def float_shape(b,*groups):
    names=operands(b,*groups)
    return all(names) and output(b) is not None and output(b).size==4 and all(b.values[n].size==4 for n in names) and names

def emit_FADD(b,check=False):
    names=float_shape(b,'Ra/Sa','Rc/Sc/URc')
    if check:return bool(names)
    mode,flush,(a,c)=float_inputs(b,names)
    float_result(b,mode,flush,lambda _:f'local r:4 = {a} f+ {c};')

def emit_FMUL(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb')
    if check:return bool(names)
    mode,flush,(a,c)=float_inputs(b,names)
    scale=enum_cases(b,'scale',list(SCALES))
    # The exact product of two fp32 values fits in fp64, so scaling there and
    # narrowing once rounds exactly as hardware does.
    def core(label):
        k=SCALES[label] if label else 0
        if not k:return f'local r:4 = {a} f* {c};'
        return f'local r:4 = float2float((float2float({a}) f* float2float({c})) f* {(1023+k)<<52:#x}:8);'
    float_result(b,mode,flush,core,('scale',scale) if scale!={None:None} else None)

def emit_FFMA(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb','Rc/Sc/URc')
    if check:return bool(names)
    mode,flush,(a,c,d)=float_inputs(b,names)
    # The fp32 product is exact in fp64, so only the fp64 sum and the final
    # narrowing round: this double rounding can differ from hardware's single
    # rounding in rare midpoint cases. Readability wins over that last bit.
    core=lambda _:(f'local x:8 = float2float({a}); local y:8 = float2float({c}); local z:8 = float2float({d}); '
                   'local r:4 = float2float(x f* y f+ z);')
    float_result(b,mode,flush,core)

def emit_FSEL(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb')
    p=operands(b,'Pp')[0]
    if check:return bool(names and p) and set(b.outputs)=={'Rd'}
    _,_,(a,c)=float_inputs(b,names)
    result=b.local(4,a)
    b.body += [f'if ({b.flag(p)} != 0) goto <selected>;',f'{result.symbol} = {c};',
               '<selected>',f'{output(b).symbol} = {result.symbol};']


def emit_FMNMX(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb')
    p=operands(b,'Pp')[0]
    if check:return bool(names and p) and set(b.outputs)=={'Rd'}
    # .NAN and .XORSIGN require separate hardware evidence.
    require(b,'nan',['nonan']);require(b,'xorsign',['noxorsign'])
    _,_,(a,c)=float_inputs(b,names)
    predicate=b.flag(p)
    # Numeric input wins over NaN. Equal signed zeros use OR for min and AND
    # for max; this preserves -0 for min and +0 for max in either source order.
    result=b.local(4,c)
    b.body += [f'if (!nan({a})) goto <numeric_a>;',
               f'if (!nan({c})) goto <minmaxdone>;',
               f'{result.symbol} = 0x7fffffff;','goto <minmaxdone>;',
               '<numeric_a>',f'if (nan({c})) goto <choosea>;',
               f'if ((({a} | {c}) & 0x7fffffff) != 0) goto <compare>;',
               f'if ({predicate} == 0) goto <maxzero>;',
               f'{result.symbol} = {a} | {c};','goto <minmaxdone>;',
               '<maxzero>',f'{result.symbol} = {a} & {c};','goto <minmaxdone>;',
               '<compare>',f'if (({a} f< {c}) != {predicate}) goto <minmaxdone>;',
               '<choosea>',f'{result.symbol} = {a};','<minmaxdone>',
               f'{output(b).symbol} = {result.symbol};']


def emit_FSETP(b,check=False):
    names=operands(b,'Ra/Sa','Rb/Sb/URb')
    if check:return all(names) and 'fcomp' in b.values and output(b,'Pu') is not None and all(b.values[n].size==4 for n in names)
    _,_,(x,y)=float_inputs(b,names)
    unordered=f'(nan({x}) || nan({y}))'
    ordered={'LT':f'{x} f< {y}','EQ':f'{x} f== {y}','LE':f'{x} f<= {y}','GT':f'{y} f< {x}',
             'NE':f'!{unordered} && ({x} f!= {y})','GE':f'{y} f<= {x}'}
    tests={**ordered,**{l+'U':f'{unordered} || ({e})' for l,e in ordered.items() if l!='NE'},
           'NEU':f'{x} f!= {y}','NUM':f'!{unordered}','NAN':unordered,'T':'1:1','F':'0:1'}
    en=enum_cases(b,'fcomp',list(tests))
    cond=b.select_code(('fcomp',),{(v,):f'local t:1 = {tests[l]}; export t;' for l,v in en.items()})
    if 'bop' not in b.values:
        b.body.append(f'{output(b,"Pu").symbol} = {cond};');return
    pv=b.flag(operands(b,'Pp')[0]);yes=combine(b,cond,pv);no=combine(b,b.expression(1,f'!{cond}').symbol,pv)
    yes=b.local(1,yes).symbol;no=b.local(1,no).symbol
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {yes if name in ("Pu","UPu") else no};')


def emit_ISETP(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and 'icmp' in b.values and 'bop' in b.values and b.values[a].size==b.values[c].size
    if 'ex' in b.values or any(n in b.values for n in ('Pr','UPr')):b.reject()
    require(b,'fmt',['U32','S32'])
    cond=comparison(b,b.flag(a),b.flag(c)); pv=b.flag(p)
    yes=combine(b,cond,pv)
    # Use unique labels for the complementary predicate's boolean combination.
    inv=b.expression(1,f'!{cond}')
    no=combine(b,inv.symbol,pv)
    # Capture both results before either destination overwrites a predicate input.
    yes=b.local(1,yes).symbol;no=b.local(1,no).symbol
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {yes if name in ("Pu","UPu") else no};')


def emit_IMNMX(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and output(b) is not None and all(b.values[n].size==4 for n in (a,c)) and output(b).size==4 and set(b.outputs)=={'Rd'}
    require(b,'fmt',['U32','S32']); av,cv,pv=b.flag(a),b.flag(c),b.flag(p)
    fmt=b.k.operand_types.get('fmt');formats=b.g.arch.enums.get(fmt.type,{}) if fmt else {}
    less=b.local(1,b.select('fmt',{value:f'{av} {"s<" if label=="S32" else "<"} {cv}' for label,value in formats.items()},1) if formats else f'{av} < {cv}')
    res=b.local(4,cv)
    b.body += [f'if ({less.symbol} != {pv}) goto <minselected>;',f'{res.symbol} = {av};','<minselected>',f'{output(b).symbol} = {res.symbol};']


def emit_LEA(b,check=False):
    a,c=operands(b,'Ra','Rb/Sb');scale=operands(b,'scaleU5')[0]
    if check:return all((a,c,scale)) and output(b) is not None and 'LO' in b.g.arch.enums.get(b.k.operand_types['hilo'].type,{})
    if 'X' in b.values:b.reject()
    require(b,'hilo',['LO']);require(b,'sx32',['nosx32','NOSX32'])
    if any(n in b.values for n in ('Pp','Pr','Rc','Sc','UPp','UPr','URc')):b.reject()
    for name in b.outputs:
        if name not in ('Rd','URd'):b.allow(name,lambda v:v==7)
    t=b.local(8,f'(zext({b.flag(a)}) << {b.values[scale].symbol}) + zext({b.flag(c)})')
    b.body.append(f'{output(b).symbol} = {t.symbol}:4;')


def emit_S2R(b,check=False):
    src=b.g.source(b.k,'SRa')
    if check:return output(b) is not None and src and src[0]=='field' and src[2]==1
    out=output(b);sr=b.g.specialsem(src[1],out.size);b.refs.append(('sub',sr))
    b.body.append(f'{out.symbol} = {sr};')

emit_CS2R=emit_S2R


def emit_BRA(b,check=False):
    if check:return 'sImm' in b.values and 'Pp' in b.values
    require(b,'depth',[default(b,'depth')]);require(b,'cond',[default(b,'cond'),'U'])
    if any(n in b.values for n in ('URb','UPq')):b.reject()
    atom=b.k.operand_types['sImm'];target=b.g.branch_target(b.k,atom);b.refs.append(('sub',target))
    b.body.append(f'if ({b.flag("Pp")} != 0) goto {target};')


def emit_EXIT(b,check=False):
    if check:return True
    require(b,'mode',['noexit_mode'])
    if any(n in b.values for n in ('Ra','URa')):b.reject()
    if 'Pp' in b.values:b.body.append(f'if ({b.flag("Pp")} == 0) goto <exitskip>;')
    b.body.append('return [0:8];')
    if 'Pp' in b.values:b.body.append('<exitskip>')


def emit_CALL(b,check=False):
    if check:return 'Pp' in b.values and any(n in b.values for n in ('sImm','Sa','Ra','URa'))
    require(b,'depth',['NOINC','noinc','nodepth'])
    b.body.append(f'if ({b.flag("Pp")} == 0) goto <callskip>;')
    if 'sImm' in b.values:
        atom=b.k.operand_types['sImm'];target=b.g.branch_target(b.k,atom);b.refs.append(('sub',target));b.body.append(f'call {target};')
    else:
        name=operands(b,'Sa/Ra/URa')[0];target=b.cast(name,8)
        b.body.append(f'call [{target}];')
    b.body.append('<callskip>')


def emit_RET(b,check=False):
    if check:return any(n in b.values for n in ('Ra','URa')) and 'Pp' in b.values
    require(b,'addr',['ABS']);require(b,'depth',['NODEC'])
    name=operands(b,'Ra/URa')[0];off=operands(b,'Ra_offset/Sa_offset')[0]
    target=b.local(8,f'{b.cast(name,8)} + {b.cast(off,8,True) if off else "0"}')
    b.body.append(f'if ({b.flag("Pp")} == 0) goto <retskip>;');b.body += [f'return [{target.symbol}];','<retskip>']


def memory(b,op,check):
    load=op.startswith('LD');space='cbank' if op=='LDC' else 'shared' if op in ('LDS','STS') else 'localmem' if op in ('LDL','STL') else 'ram'
    if check:return ('sz' in b.values and 'Ra' in b.values and (output(b) is not None if load else 'Rb' in b.values)
                     and bool(set(b.g.arch.enums[b.k.operand_types['sz'].type]) & {'U8','S8','U16','S16','32','64','128'}))
    if any(n in b.values for n in ('Rd2','Rb2')):b.reject()
    if 'memoryDescriptor' in b.values:
        # The implicit descriptor form emitted for ordinary CUDA global pointers
        # addresses ram directly. Explicit descriptors need a resource model.
        if 'e_desc' not in b.values:b.reject()
        else:b.allow('e_desc',lambda v:v==0)
    elif any(n in b.values for n in ('Ra_URb','Ra_URc')):b.reject()
    if 'ad' in b.values:require(b,'ad',['IA'])
    if 'sp2' in b.values:require(b,'sp2',['nosp2'])
    if 'Pnz' in b.values:
        b.allow('Pnz',lambda v:v==7)
    # Predicate-result variants are not assumed to mean successful/nonzero loads.
    for name in b.outputs:
        if name not in ('Rd','URd'):b.allow(name,lambda v:v==7)
    base=b.values['Ra'];off=operands(b,'Ra_offset/Sa_offset')[0]
    base_expr=base.symbol if base.size==8 else f'zext({base.symbol})'
    if 'stride' in b.values:
        en=b.g.arch.enums[b.k.operand_types['stride'].type]
        factors={en[label]:f'{factor}:8' for label,factor in [('X1',1),('X4',4),('X8',8),('X16',16)] if label in en}
        b.allow('stride',lambda v:v in factors)
        factor=b.select('stride',factors,8)
        base_expr=f'({base_expr} * {factor})'
    offset=b.values[off] if off else None
    off_expr=(offset.symbol if offset.size==8 else f'sext({offset.symbol})') if offset else '0:8'
    addr=b.expression(8,f'{base_expr} + {off_expr}')
    if space=='cbank':
        bank=operands(b,'Sa_bank')[0]
        if not bank:b.reject();return
        low=b.expression(4,f'{addr.symbol}:4')
        addr=b.expression(8,f'(zext({b.values[bank].symbol}) << 32) | zext({low.symbol})')
    fn='sass_memory_order';b.g.pcodeops.add(fn)
    controls=[b.values[n].symbol for n in ('sem','sco','cop','cop2','private') if n in b.values]
    b.body.append(f'{fn}({b.k.order}:4'+(' , '+', '.join(controls) if controls else '')+');')
    atom=b.k.operand_types['sz'];en=b.g.arch.enums[atom.type]
    result=output(b) if load else b.values['Rb']
    cases={}
    for label,width,signed in [('U8',1,False),('S8',1,True),('U16',2,False),('S16',2,True),('32',4,False),('64',8,False),('128',16,False)]:
        if label not in en or width>result.size:continue
        if load:
            code=f'local v:{width} = *[{space}]:{width} {addr.symbol}; '
            expr='v' if width==result.size else f'{"sext" if signed else "zext"}(v)'
            code+=f'local t:{result.size} = {expr}; export t;'
        else:
            value=result.symbol if result.size==width else f'{result.symbol}:{width}'
            code=f'local v:{width} = {value}; *[{space}]:{width} {addr.symbol} = v;'
        cases[(en[label],)]=code
    b.allow('sz',lambda v:(v,) in cases)
    table=b.select_code(('sz',),cases)
    b.body.append(f'build {table};')
    if load:
        b.body.append(f'{result.symbol} = {table};')

for _op in ('LDG','STG','LDS','STS','LDL','STL','LDC'):
    globals()['emit_'+_op]=lambda b,check=False,op=_op:memory(b,op,check)
