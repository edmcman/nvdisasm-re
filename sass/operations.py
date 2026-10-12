"""Class-specific native overrides and explicit instruction primitives.

Unsupported selector combinations decode to complete-input opaque operations.
This module does not equate PTX syntax with undocumented SASS variants.
"""
from dataclasses import dataclass
from itertools import product
from sass import fpops

OPERATION_FAMILIES = set('IADD3 IMAD LOP3 SHF BMSK LEA IABS IMNMX ISETP SEL PRMT MOV S2R CS2R LDC FADD FMUL FFMA FSETP F2I F2FP I2F I2FP MUFU LDG STG LDS STS LDL STL BRA EXIT CALL RET BSSY BSYNC BAR SHFL VOTE ULDC NOP'.split())
ALIASES = dict(UIADD3='IADD3', UIMAD='IMAD', USHF='SHF', ULEA='LEA',
               UISETP='ISETP', ULOP3='LOP3', USEL='SEL', UMOV='MOV', VIADD='IADD',
               UF2FP='F2FP', **{'HFMA2.MMA':'HFMA2_MMA'})
OPERATION_FAMILIES.update(ALIASES)
OPERATION_FAMILIES.update(('IADD','FSEL','FMNMX','FMNMX3','DADD','DMUL','DFMA','DSETP','F2IP','I2IP','HADD2','HFMA2','HMUL2','HMNMX2','BREV','POPC','FLO','SGXT','IDP','QSPC','UP2UR','P2R','R2P','VIMNMX','VIMNMX3','VIADDMNMX','VABSDIFF4'))

PRIMITIVES = set('FADD FMUL FFMA FSETP F2I I2F I2FP MUFU PRMT SHFL VOTE BAR BSSY BSYNC QSPC'.split())

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
            span=value.size//REG_FILES[file][1];zero=b.g.register_zero(b.k,name)
            limit=b.g.register_limit(b.k,name)
            if span>1 or file=='UR':
                b.allow(name,lambda v,z=zero,s=span,n=limit:v==z or v<max(0,n-s+1))


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
    native_pattern=pattern(b.native_constraints,b.native_refs)
    if b.native_supported and native_pattern:
        # Share arithmetic bodies independently of each class's opaque ABI.
        calculation=b.g.subtable('native',[('""',pattern([],refs),'',native)])
        if b.selector_patterns:
            for clauses in b.selector_patterns:
                root=pattern([*b.native_constraints,*clauses],[*b.native_refs,('sub',calculation)])
                if root:constructors.append(('""',root,'',f'build {calculation};'))
        else:
            constructors.append(('""',pattern(b.native_constraints,[*b.native_refs,('sub',calculation)]),'',f'build {calculation};'))
    if not constructors or b.native_constraints or b.native_refs or b.selector_patterns:
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
        # QSPC has a deliberately narrow runtime implementation. Unsupported
        # selectors/forms must use the opaque ABI instead of reaching a
        # primitive that can only reject them after execution starts.
        dispatch(b,'opaque' if op=='QSPC' else 'prim')
        reason=('decode-time selector guards fall back to opaque semantics'
                if op=='QSPC' else
                'decode-time selector guards fall back to runtime primitives')
        return 'native',reason
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
    result=[]
    for group in groups:
        candidates=group.split('/')+['U'+n for n in group.split('/')]
        # A CX operand exposes its UR pair and offset as child operands. Those
        # children describe the address; arithmetic must consume the loaded
        # compound value instead.
        compound=next((n for n in candidates if n in b.values and
                       b.k.operand_types.get(n) and b.k.operand_types[n].type=='CX'),None)
        result.append(compound or next((n for n in candidates if n in b.values),None))
    return result


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


def carry_choice(b,fast,slow,predicates):
    """Keep compact arithmetic when all carry destinations are discarded."""
    import re
    from sass.gen_sleigh import pattern
    predicates=[n for n in predicates if n in b.outputs]
    if not predicates:b.body+=fast;return
    clauses=[]
    for name in predicates:clauses+=b.g.values_where(b.k,name,lambda v:v==7)
    constructors=[]
    for constraints,lines in [(clauses,fast),([],slow)]:
        code=' '.join(lines);names=set(re.findall(r'\b\w+\b',code))
        refs=[r for r in b.refs if r[1] in names]
        constructors.append(('""',pattern(constraints,refs),'',code))
    table=b.g.subtable('carry',constructors);b.refs.append(('sub',table));b.body.append(f'build {table};')


def modified_word(b,name,symbol=None):
    """Unsigned 33-bit addend; negate retains the +1 carry even for -0."""
    value=symbol or b.values[name].symbol
    if name+'@negate' in b.values:
        return b.select(name+'@negate',{0:f'zext({value})',1:f'zext(~{value}) + 1:8'},8)
    if name+'@invert' in b.values:
        return b.select(name+'@invert',{0:f'zext({value})',1:f'zext(~{value})'},8)
    return b.expression(8,f'zext({value})').symbol


def exclusive_modifiers(b,a,c,attribute):
    keys=[n+'@'+attribute for n in (a,c)]
    if all(n in b.values for n in keys):
        clauses=[b.g.values_where(b.k,tuple(n.split('@')),lambda v:v==0) for n in keys]
        if all(len(c)==1 for c in clauses):b.native_constraints.append(clauses[0][0]+clauses[1][0])
        else:b.allow(keys[0],lambda v:v==0)


def emit_IMAD(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:
        if not (all((a,c,d)) and output(b) is not None and all(b.values[n].size<=8 for n in (a,c,d))):return False
        return True
    extended='X' in b.values
    if 'x' in b.values:require(b,'x',['nox','NOX'])
    if any(n in b.values for n in ('Pq','UPq')):b.reject()
    carryin=operands(b,'Pp')[0]
    if bool(carryin)!=extended:b.reject()
    require(b,'fmt',['U32','S32'])
    for name in b.outputs:
        if name not in ('Rd','URd','Pu','UPu'):b.reject()
    aa,bb=b.flag(a),b.flag(c)
    wide=b.k.operand_types.get('wide');labels=b.g.arch.enums.get(wide.type,{}) if wide else {}
    mode=next(iter(labels),'LO');rd=output(b)
    initial=len(b.body)
    if mode=='LO':
        # Low-word multiplication and addition are independent of signedness.
        product=b.local(4,f'{aa} * {bb}')
        addend=b.flag(d)
        if b.values[d].size>4:addend=b.local(4,f'{addend}:4').symbol
        result=b.local(4,f'{product.symbol} + {addend}')
        if extended:result=b.local(4,f'{result.symbol} + zext({b.flag(carryin)})')
    else:
        fmt=b.k.operand_types.get('fmt');formats=b.g.arch.enums.get(fmt.type,{}) if fmt else {}
        x=b.select('fmt',{value:f'{"sext" if label=="S32" else "zext"}({aa})'
                          for label,value in formats.items()},8) if formats else b.local(8,f'zext({aa})').symbol
        y=b.select('fmt',{value:f'{"sext" if label=="S32" else "zext"}({bb})'
                          for label,value in formats.items()},8) if formats else b.local(8,f'zext({bb})').symbol
        product=b.local(8,f'{x} * {y}')
        rc=b.flag(d)
        atom=b.k.operand_types[d]
        pair_addend=(atom.type in ('Register','UniformRegister') and b.values[d].size==8)
        if mode=='HI' and not pair_addend and b.values[d].size<=4:
            # Narrow non-pair addends are added to the extracted high word.
            # A 64-bit constant-bank value follows the full-width datapath,
            # as established by the SM89 GPU probe.
            basewide=b.local(8,f'{product.symbol} >> 32')
            base=b.local(4,f'{basewide.symbol}:4')
            if b.values[d].size>4:rc=b.local(4,f'{rc}:4').symbol
            elif b.values[d].size<4:rc=b.local(4,f'zext({rc})').symbol
            full=b.local(8,f'zext({base.symbol}) + zext({rc})' +
                         (f' + zext({b.flag(carryin)})' if extended else ''))
            result=b.local(4,f'{full.symbol}:4')
            overflow=f'{full.symbol} >= 0x100000000:8'
        else:
            if b.values[d].size<8:rc=b.local(8,f'zext({rc})').symbol
            summed=b.local(8,f'{product.symbol} + {rc}')
            result=b.local(8,f'{summed.symbol} + zext({b.flag(carryin)})') if extended else summed
            full=result
            if mode=='HI':result=b.local(8,f'{result.symbol} >> 32')
            overflow=f'({summed.symbol} < {product.symbol})'
            if extended:overflow+=f' || ({full.symbol} < {summed.symbol})'
    fast=b.body[initial:]+[f'{rd.symbol} = {result.symbol}{":4" if result.size>rd.size else ""};']
    b.body=b.body[:initial]
    if mode=='LO':b.body+=fast;return
    predicate=output(b,'Pu')
    if predicate:
        slow=fast[:-1]+[f'local cout:1 = {overflow};',fast[-1],f'{predicate.symbol} = cout;']
        carry_choice(b,fast,slow,['Pu','UPu'])
    else:b.body+=fast


def emit_IADD3(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,c,d)) and output(b) is not None and all(b.values[n].size==4 for n in (a,c,d))
    extended='X' in b.values
    p,q=operands(b,'Pp','Pq')
    if extended and not (p and q) or not extended and (p or q):b.reject()
    exclusive_modifiers(b,a,c,'invert' if extended else 'negate')
    aa,bb,cc=(b.flag(n) for n in (a,c,d))
    fast=[f'local first:4 = {aa} + {bb};',f'local total:4 = first + {cc};']
    if extended:fast += [f'total = total + zext({b.flag(p)}) + zext({b.flag(q)});']
    fast += [f'{output(b).symbol} = total;']
    av,bv,cv=[modified_word(b,n) for n in (a,c,d)]
    slow=[f'local total:8 = {av} + {bv} + {cv}'+(f' + zext({b.flag(p)}) + zext({b.flag(q)})' if extended else '')+';',
          'local cout1:1 = total >= 0x100000000:8; local cout2:1 = total >= 0x200000000:8;',
          f'{output(b).symbol} = total:4;']
    # Hardware gives Pu priority when the two predicate destinations alias.
    for name in ('Pv','UPv','Pu','UPu'):
        if name in b.outputs:slow.append(f'{b.outputs[name].symbol} = {"cout1" if name.endswith("u") else "cout2"};')
    carry_choice(b,fast,slow,['Pu','UPu','Pv','UPv'])


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


def _unary_word_source(b):
    if 'Sb' in b.values and b.k.operand_types['Sb'].type=='CX':
        return 'Sb'  # CX is an explicit runtime operand, not just its URb child.
    return next((n for n in ('Rb','URb','Sb') if n in b.values),None)


def emit_BREV(b,check=False):
    """Reverse the bit order of one 32-bit integer source."""
    src=_unary_word_source(b);dst=output(b)
    if check:
        return src is not None and dst is not None and b.values[src].size==4 and dst.size==4 and b.values[src].kind!='runtime-primitive'
    value=b.flag(src)
    for shift,mask in ((1,0x55555555),(2,0x33333333),(4,0x0f0f0f0f),(8,0x00ff00ff)):
        value=b.local(4,f'(({value} >> {shift}) & {mask:#x}) | (({value} & {mask:#x}) << {shift})').symbol
    value=b.local(4,f'({value} >> 16) | ({value} << 16)').symbol
    b.body.append(f'{dst.symbol} = {value};')


def emit_POPC(b,check=False):
    """Count the set bits in one 32-bit integer source using SWAR arithmetic."""
    src=_unary_word_source(b);dst=output(b)
    if check:
        return src is not None and dst is not None and b.values[src].size==4 and dst.size==4 and b.values[src].kind!='runtime-primitive'
    value=b.flag(src)
    value=b.local(4,f'{value} - (({value} >> 1) & 0x55555555)').symbol
    value=b.local(4,f'({value} & 0x33333333) + (({value} >> 2) & 0x33333333)').symbol
    value=b.local(4,f'(({value} + ({value} >> 4)) & 0x0f0f0f0f)').symbol
    value=b.local(4,f'{value} + ({value} >> 8)').symbol
    value=b.local(4,f'{value} + ({value} >> 16)').symbol
    value=b.local(4,f'{value} & 0x3f').symbol
    b.body.append(f'{dst.symbol} = {value};')


def emit_FLO(b,check=False):
    """Find the most-significant set-bit index, or produce a normalization shift."""
    src=_unary_word_source(b);dst=output(b)
    if check:
        if src is None or dst is None or dst.size!=4 or b.values[src].size!=4 or b.values[src].kind=='runtime-primitive':return False
        return True
    if 'Pu' in b.outputs:
        b.allow('Pu',lambda v:v==b.g.arch.enums[b.k.operand_types['Pu'].type]['PT'])
    require(b,'fmt',['S32','U32','32'])
    require(b,'sh',['nosh','SH'])
    value=b.flag(src)
    def flo_code(shift_mode):
        code=f'local x:4 = {value}; local n:4 = 0; '
        for shift,mask in ((16,0xffff0000),(8,0xff000000),(4,0xf0000000),(2,0xc0000000),(1,0x80000000)):
            code+=f'if ((x & {mask:#x}) != 0) goto <keep{shift}>; n = n | {shift}; x = x << {shift}; <keep{shift}> '
        code+='local r:4 = 0xffffffff; if (x == 0) goto <done>; '
        code+=('r = n;' if shift_mode else 'r = 31 - n;')+' <done> export r;'
        return code
    sh=b.k.operand_types.get('sh')
    if sh:
        modes=b.g.arch.enums[sh.type]
        result=b.select_code(('sh',),{(modes['nosh'],):flo_code(False),(modes['SH'],):flo_code(True)})
    else:result=b.expression(4,'0xffffffff')
    b.body.append(f'{dst.symbol} = {result};')


def emit_SGXT(b,check=False):
    """Select a byte with the low two index bits and sign extend it."""
    index=operands(b,'Ra')[0];src=operands(b,'Rb/Sb')[0];dst=output(b)
    if check:
        ok=all((index,src)) and dst is not None and dst.size==4 and b.values[index].size==4 and b.values[src].size==4 and b.values[src].kind!='runtime-primitive'
        if not ok:b.unsupported_reason='SGXT requires 32-bit index and source values'
        return ok
    require(b,'cw',['C']);require(b,'fmt',['S32','32'])
    idx=b.flag(index);value=b.flag(src)
    shift=b.local(4,f'({idx} & 3) << 3').symbol
    shifted=b.local(4,f'{value} >> {shift}').symbol
    byte=b.local(1,f'{shifted}:1').symbol
    b.body.append(f'{dst.symbol} = sext({byte});')


def emit_IDP(b,check=False):
    """Packed integer dot products with a 32-bit accumulator (IDP.2A/4A)."""
    is_2a=b.k.name.startswith('idp_2a__')
    is_4a=b.k.name.startswith('idp_4a__')
    if not (is_2a or is_4a):
        if check:b.unsupported_reason='unrecognized IDP class shape'
        return False if check else None
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc');dst=output(b)
    if check:
        ok=(all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='IDP requires materialized 32-bit sources'
        return ok
    require(b,'mode',['2A.LO','2A.HI'] if is_2a else ['4A'])
    require(b,'SrcAFmt',['S16','U16'] if is_2a else ['S8','U8'])
    require(b,'SrcBFmt',['S8','U8'])
    av=b.flag(a);bv=b.flag(bb);cv=b.flag(c)
    cases={}
    afmts=enum_cases(b,'SrcAFmt',['S16','U16'] if is_2a else ['S8','U8'])
    bfmts=enum_cases(b,'SrcBFmt',['S8','U8'])
    modes=enum_cases(b,'mode',['2A.LO','2A.HI'] if is_2a else ['4A'])
    for asigned,aval in afmts.items():
        for bsigned,bval in enum_cases(b,'SrcBFmt',['S8','U8']).items():
            mode_cases={}
            for mode,mval in modes.items():
                code=f'local aa:4 = {av}; local bbv:4 = {bv}; local acc:4 = {cv}; '
                count=2 if is_2a else 4
                aw=16 if is_2a else 8
                bwstart=2 if is_2a and mode.endswith('HI') else 0
                for i in range(count):
                    ai=i*aw;bi=(i+bwstart)*8
                    code+=f'local ashr{i}:4 = aa >> {ai}; local bshr{i}:4 = bbv >> {bi}; '
                    if is_2a:
                        code+=f'local ahalf{i}:2 = ashr{i}[0,16]; '
                        ax=f'sext(ahalf{i})' if asigned=='S16' else f'zext(ahalf{i})'
                    else:
                        code+=f'local abyte{i}:1 = ashr{i}[0,8]; '
                        ax=f'sext(abyte{i})' if asigned=='S8' else f'zext(abyte{i})'
                    code+=f'local bbyte{i}:1 = bshr{i}[0,8]; '
                    bx=f'sext(bbyte{i})' if bsigned=='S8' else f'zext(bbyte{i})'
                    code+=f'acc = acc + ({ax} * {bx}); '
                code+='export acc;'
                mode_cases[(mval,)]=code
            for (mv,),code in mode_cases.items():
                cases[(mv,aval,bval)]=code
    b.allow_combinations(('mode','SrcAFmt','SrcBFmt'),cases.keys())
    result=b.select_code(('mode','SrcAFmt','SrcBFmt'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_VIMNMX3(b,check=False):
    """Per-lane three-way integer min/max, with the CuLifter RELU option."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/URc/Sc');dst=output(b)
    if check:
        ok=(b.k.name.startswith('vimnmx3__') and all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VIMNMX3 requires three materialized 32-bit values'
        return ok
    require(b,'fmt',['S32','U32','S16x2','U16x2'])
    require(b,'relu',['norelu','RELU'])
    aval,bval,cval=map(b.flag,(a,bb,c));pred=b.flag('Pp')
    formats=enum_cases(b,'fmt',['S32','U32','S16x2','U16x2'])
    relus=enum_cases(b,'relu',['norelu','RELU'])
    cases={}
    for fmt,fv in formats.items():
        width=16 if '16x2' in fmt else 32
        signed=fmt.startswith('S')
        compare='s' if signed else ''
        lane_count=32//width
        for relu,rv in relus.items():
            if relu=='RELU' and not signed:continue
            code=f'local aa:4 = {aval}; local bbv:4 = {bval}; local cc:4 = {cval}; local result:4 = 0; '
            for lane in range(lane_count):
                off=lane*width
                if width==32:
                    x,y,z='aa','bbv','cc'
                else:
                    code+=f'local xa{lane}:2 = aa[{off},{width}]; local yb{lane}:2 = bbv[{off},{width}]; local zc{lane}:2 = cc[{off},{width}]; '
                    x=f'{"sext" if signed else "zext"}(xa{lane})'
                    y=f'{"sext" if signed else "zext"}(yb{lane})'
                    z=f'{"sext" if signed else "zext"}(zc{lane})'
                code+=f'local x{lane}:4 = {x}; local y{lane}:4 = {y}; local z{lane}:4 = {z}; '
                code+=f'local ab{lane}:4 = 0; local abc{lane}:4 = 0; '
                choose_x=f'(({pred} == 1 && x{lane} {compare}< y{lane}) || ({pred} != 1 && x{lane} {compare}> y{lane}))'
                choose_ab=f'(({pred} == 1 && ab{lane} {compare}< z{lane}) || ({pred} != 1 && ab{lane} {compare}> z{lane}))'
                code+=f'if {choose_x} goto <abx{lane}>; ab{lane} = y{lane}; goto <abd{lane}>; <abx{lane}> ab{lane} = x{lane}; <abd{lane}> '
                code+=f'if {choose_ab} goto <abcx{lane}>; abc{lane} = z{lane}; goto <abcd{lane}>; <abcx{lane}> abc{lane} = ab{lane}; <abcd{lane}> '
                if relu=='RELU':
                    if width==32:
                        code+=f'if (abc{lane} s< 0) goto <relu_zero{lane}>; result = abc{lane}; goto <relu_done{lane}>; <relu_zero{lane}> result = 0; <relu_done{lane}> '
                    else:
                        lane_size=width//8
                        code+=f'local clamped{lane}:{lane_size} = 0; if (abc{lane} s< 0) goto <relu_zero{lane}>; clamped{lane} = abc{lane}[0,{width}]; goto <relu_done{lane}>; <relu_zero{lane}> clamped{lane} = 0; <relu_done{lane}> result = result | (zext(clamped{lane}) << {off}); '
                elif width==32:code+=f'result = abc{lane}; '
                else:code+=f'result = result | (zext(abc{lane}[0,{width}]) << {off}); '
            code+='export result;'
            cases[(fv,rv)]=code
    b.allow_combinations(('fmt','relu'),cases.keys())
    result=b.select_code(('fmt','relu'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_VIMNMX(b,check=False):
    """Scalar or packed integer min/max; PT selects min and !PT selects max."""
    a,bb=operands(b,'Ra','Rb/URb/Sb');dst=output(b)
    if check:
        ok=(all((a,bb)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb)))
        if not ok:b.unsupported_reason='VIMNMX requires two materialized 32-bit values'
        return ok
    require(b,'fmt',['S32','U32','S16x2','U16x2','S8x4','U8x4'])
    require(b,'relu',['norelu','RELU'])
    for pred in ('Pu','Pv'):
        if pred in b.outputs:
            atom=b.k.operand_types[pred];pt=b.g.arch.enums[atom.type]['PT']
            b.allow(pred,lambda v,p=pt:v==p)
    av,bv=map(b.flag,(a,bb));pred=b.flag('Pp')
    formats=enum_cases(b,'fmt',['S32','U32','S16x2','U16x2','S8x4','U8x4'])
    relus=enum_cases(b,'relu',['norelu','RELU'])
    cases={}
    for fmt,fv in formats.items():
        width=8 if '8x4' in fmt else 16 if '16x2' in fmt else 32
        signed=fmt.startswith('S');compare='s' if signed else ''
        lane_count=32//width
        for relu,rv in relus.items():
            if relu=='RELU' and not signed:continue
            code=f'local aa:4 = {av}; local bbv:4 = {bv}; local result:4 = 0; '
            for lane in range(lane_count):
                off=lane*width
                if width==32:x,y='aa','bbv'
                else:
                    lane_size=width//8
                    code+=f'local xa{lane}:{lane_size} = aa[{off},{width}]; local yb{lane}:{lane_size} = bbv[{off},{width}]; '
                    x=f'{"sext" if signed else "zext"}(xa{lane})'
                    y=f'{"sext" if signed else "zext"}(yb{lane})'
                code+=f'local x{lane}:4 = {x}; local y{lane}:4 = {y}; local z{lane}:4 = 0; '
                choose_x=f'(({pred} == 1 && x{lane} {compare}< y{lane}) || ({pred} != 1 && x{lane} {compare}> y{lane}))'
                code+=f'if {choose_x} goto <take_x{lane}>; z{lane} = y{lane}; goto <done{lane}>; <take_x{lane}> z{lane} = x{lane}; <done{lane}> '
                if relu=='RELU':
                    if width==32:
                        code+=f'if (z{lane} s< 0) goto <relu_zero{lane}>; result = z{lane}; goto <relu_done{lane}>; <relu_zero{lane}> result = 0; <relu_done{lane}> '
                    else:
                        lane_size=width//8
                        code+=f'local clamped{lane}:{lane_size} = 0; if (z{lane} s< 0) goto <relu_zero{lane}>; clamped{lane} = z{lane}[0,{width}]; goto <relu_done{lane}>; <relu_zero{lane}> clamped{lane} = 0; <relu_done{lane}> result = result | (zext(clamped{lane}) << {off}); '
                elif width==32:code+=f'result = z{lane}; '
                else:code+=f'result = result | (zext(z{lane}[0,{width}]) << {off}); '
            code+='export result;'
            cases[(fv,rv)]=code
    b.allow_combinations(('fmt','relu'),cases.keys())
    result=b.select_code(('fmt','relu'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_VIADDMNMX(b,check=False):
    """Per-lane modular add followed by predicate-selected min/max."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/URc/Sc');dst=output(b)
    if check:
        ok=(all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VIADDMNMX requires three materialized 32-bit values'
        return ok
    require(b,'fmt',['S32','U32','S16x2','U16x2'])
    require(b,'relu',['norelu','RELU'])
    av,bv,cv=map(b.flag,(a,bb,c));pred=b.flag('Pp')
    formats=enum_cases(b,'fmt',['S32','U32','S16x2','U16x2'])
    relus=enum_cases(b,'relu',['norelu','RELU'])
    cases={}
    for fmt,fv in formats.items():
        width=16 if '16x2' in fmt else 32
        lane_size=width//8;signed=fmt.startswith('S');compare='s' if signed else ''
        lane_count=32//width
        for relu,rv in relus.items():
            if relu=='RELU' and not signed:continue
            code=f'local aa:4 = {av}; local bbv:4 = {bv}; local cc:4 = {cv}; local result:4 = 0; '
            for lane in range(lane_count):
                off=lane*width
                if width==32:
                    z='cc';raw_sum='aa + bbv'
                    code+=f'local z{lane}:4 = {z}; '
                else:
                    code+=f'local xa{lane}:{lane_size} = aa[{off},{width}]; local yb{lane}:{lane_size} = bbv[{off},{width}]; local zc{lane}:{lane_size} = cc[{off},{width}]; '
                    z=f'{"sext" if signed else "zext"}(zc{lane})'
                    raw_sum=f'xa{lane} + yb{lane}'
                    code+=f'local z{lane}:4 = {z}; '
                code+=f'local sum{lane}:{lane_size} = {raw_sum}; '
                code+=f'local sx{lane}:4 = {"sext" if signed else "zext"}(sum{lane}); local value{lane}:4 = 0; '
                choose_sum=f'(({pred} == 1 && sx{lane} {compare}< z{lane}) || ({pred} != 1 && sx{lane} {compare}> z{lane}))'
                code+=f'if {choose_sum} goto <take_sum{lane}>; value{lane} = z{lane}; goto <done{lane}>; <take_sum{lane}> value{lane} = sx{lane}; <done{lane}> '
                if relu=='RELU':
                    if width==32:
                        code+=f'if (value{lane} s< 0) goto <relu_zero{lane}>; result = value{lane}; goto <relu_done{lane}>; <relu_zero{lane}> result = 0; <relu_done{lane}> '
                    else:
                        code+=f'local clamped{lane}:{lane_size} = 0; if (value{lane} s< 0) goto <relu_zero{lane}>; clamped{lane} = value{lane}[0,{width}]; goto <relu_done{lane}>; <relu_zero{lane}> clamped{lane} = 0; <relu_done{lane}> result = result | (zext(clamped{lane}) << {off}); '
                elif width==32:code+=f'result = value{lane}; '
                else:code+=f'result = result | (zext(value{lane}[0,{width}]) << {off}); '
            code+='export result;'
            cases[(fv,rv)]=code
    b.allow_combinations(('fmt','relu'),cases.keys())
    result=b.select_code(('fmt','relu'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_VABSDIFF4(b,check=False):
    """Packed byte absolute differences or the SASS scalar ACC reduction."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/URc/Sc');dst=output(b)
    if check:
        ok=(all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VABSDIFF4 requires three materialized 32-bit inputs'
        return ok
    require(b,'fmt',['S8','U8']);require(b,'red',['SIMD_MRG','ACC'])
    if 'Pu' in b.outputs:
        atom=b.k.operand_types['Pu'];pt=b.g.arch.enums[atom.type]['PT']
        b.allow('Pu',lambda v,p=pt:v==p)
    av,bv,cv=map(b.flag,(a,bb,c))
    formats=enum_cases(b,'fmt',['S8','U8'])
    reductions=enum_cases(b,'red',['SIMD_MRG','ACC'])
    cases={}
    for fmt,fv in formats.items():
        signed=fmt=='S8';extend='sext' if signed else 'zext';compare='s' if signed else ''
        for red,rv in reductions.items():
            code=f'local aa:4 = {av}; local bbv:4 = {bv}; local result:4 = 0; '
            if red=='ACC':code+=f'local cc:4 = {cv}; '
            for lane in range(4):
                shift=lane*8
                code+=f'local ash{lane}:4 = aa >> {shift}; local bsh{lane}:4 = bbv >> {shift}; '
                code+=f'local abyte{lane}:1 = ash{lane}[0,8]; local bbyte{lane}:1 = bsh{lane}[0,8]; '
                code+=f'local ax{lane}:4 = {extend}(abyte{lane}); local bx{lane}:4 = {extend}(bbyte{lane}); local diff{lane}:4 = 0; '
                code+=f'if (ax{lane} {compare}>= bx{lane}) goto <ab{lane}>; diff{lane} = bx{lane} - ax{lane}; goto <ad{lane}>; <ab{lane}> diff{lane} = ax{lane} - bx{lane}; <ad{lane}> '
                if red=='SIMD_MRG':
                    code+=f'local byte{lane}:4 = diff{lane} & 0xff; result = result | (byte{lane} << {shift}); '
                else:code+=f'result = result + diff{lane}; '
            if red=='ACC':code+='result = result + cc; '
            code+='export result;'
            cases[(fv,rv)]=code
    result=b.select_code(('fmt','red'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_BMSK(b,check=False):
    position,width=operands(b,'Ra','Rb/Sb')
    if check:
        if width and b.k.operand_types[width].type=='CX' and b.values[width].kind=='runtime-primitive':
            b.unsupported_reason='CX header layout is not implemented for this architecture; requires a sass_operand_value provider'
        return all((position,width)) and output(b) is not None and output(b).size==4 and all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (position,width))
    require(b,'cw',['C','W'])
    modes=enum_cases(b,'cw',['C','W']);a,n=b.flag(position),b.flag(width)
    # P-code shifts by >= the word size produce zero. Clamp before adding the
    # position, so a large unsigned width cannot wrap the end position.
    cases={}
    for mode,value in modes.items():
        code=f'local pos:4 = {a}; local width:4 = {n}; '
        if mode=='W':code+='pos = pos & 31; width = width & 31; '
        else:code+='if (width <= 32) goto <width_ready>; width = 32; <width_ready> '
        code+='local result:4 = 0; if (pos >= 32) goto <done>; '
        code+='result = (0xffffffff:4 << pos) & ~(0xffffffff:4 << (pos + width)); <done> export result;'
        cases[(value,)]=code
    result=b.select_code(('cw',),cases)
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


SCALES={'noscale':0,'D2':-1,'D4':-2,'D8':-3,'M2':1,'M4':2,'M8':3}
FLUSH_LABELS=['FTZ','FMZ','noftz','nofmz','nofmz_hfma2']

def enum_cases(b,name,labels):
    """{label: value} for the labels of selector `name` this class can encode; {None: None} if absent."""
    if name not in b.values:return {None:None}
    en=b.g.arch.enums[b.k.operand_types[name].type]
    require(b,name,[l for l in labels if l in en])
    return {l:en[l] for l in labels if l in en}

def float_inputs(b,names):
    """Operand symbols after |x|/-x flags and, under exact .FTZ, denormal flushing."""
    mode='fmz' if 'fmz' in b.values else 'ftz'
    if fpops.readable(b):
        enum_cases(b,mode,FLUSH_LABELS)
        return mode,{None:None},[b.flag(n,True) for n in names]
    flush=enum_cases(b,mode,['FTZ','noftz','nofmz','nofmz_hfma2'])
    out=[]
    for name in names:
        v=b.flag(name,True)
        out.append(v if flush=={None:None} else
                   b.select(mode,{value:fpops.flush(v) if label=='FTZ' else v for label,value in flush.items()},4))
    return mode,flush,out

def float_result(b,mode,flush,core,extra=None,size=4):
    """Select the body computing r per (.FTZ, .SAT, rounding[, extra]) and assign it to Rd.

    core(rounding label, extra label) leaves the unflushed result in r."""
    rnd=enum_cases(b,'rnd',fpops.ROUNDING)
    if fpops.readable(b):rnd={None:None}
    selectors=[(n,c) for n,c in ((mode,flush),('sat',enum_cases(b,'sat',['SAT','nosat'])),('rnd',rnd),extra or (None,{None:None}))
               if n and c!={None:None}]
    cases={}
    for combo in product(*(c.items() for _,c in selectors)):
        label={n:l for (n,_),(l,_) in zip(selectors,combo)}
        code=(core(label.get('rnd','RN'),label.get(extra and extra[0]))+
              (f' r = {fpops.flush("r",size)};' if label.get(mode)=='FTZ' else '')+
              (' '+fpops.saturate(size) if label.get('sat')=='SAT' else ' ')+'export r;')
        cases[tuple(v for _,v in combo)]=code
    table=b.select_code(tuple(n for n,_ in selectors),cases)
    b.body.append(f'{output(b).symbol} = {table};')

def arithmetic(b,op,xs,size,rnd,scale=0):
    return fpops.arithmetic(op,xs,size,rnd,scale,exact=not fpops.readable(b))

def float_shape(b,*groups,size=4):
    names=operands(b,*groups)
    return all(names) and output(b) is not None and output(b).size==size and all(b.values[n].size==size for n in names) and names


def double_arithmetic(b,groups,op,check):
    names=float_shape(b,*groups,size=8)
    if check:return bool(names)
    inputs=[b.flag(n,True) for n in names]
    float_result(b,'ftz',{None:None},lambda rnd,_:arithmetic(b,op,inputs,8,rnd),size=8)


def emit_DADD(b,check=False):
    return double_arithmetic(b,('Ra/Sa','Rc/Sc/URc'),'add',check)


def emit_DMUL(b,check=False):
    return double_arithmetic(b,('Ra/Sa','Rb/Sb/URb'),'mul',check)


def emit_DFMA(b,check=False):
    return double_arithmetic(b,('Ra/Sa','Rb/Sb/URb','Rc/Sc/URc'),'fma',check)


def half_shape(b,fma):
    names=operands(b,'Ra/Sa',*(['Rb/Sb/URb','Rc/Sc/URc'] if fma else ['Rc/Sc/URc']))
    if not all(names) or output(b) is None or output(b).size!=4:return None
    for name in names:
        if name+'1' in b.values:
            if b.values[name].size not in (2,8) or b.values[name+'1'].size!=b.values[name].size:return None
        elif (b.values[name].size!=4 and not
              (not fma and b.k.name.startswith('hadd2_F32') and
               b.k.operand_types[name].type=='F16Imm' and b.values[name].size==2)):
            return None
    return names


def half_inputs(b,name,swizzle):
    if name+'1' in b.values:
        # Immediate lanes are printed high first, matching their encoded halves.
        return [b.expression(2,f'{b.values[n].symbol}:2').symbol if b.values[n].size!=2 else b.values[n].symbol
                for n in (name+'1',name)]
    value=b.values[name].symbol
    for attr,expr in [('absolute',lambda v:f'{v} & 0x7fff7fff'),('negate',lambda v:f'{v} ^ 0x80008000')]:
        key=name+'@'+attr
        if key in b.values:value=b.select(key,{0:value,1:expr(value)},4)
    modes=enum_cases(b,swizzle,['H1_H0','H0_H0','H1_H1'])
    lanes=[]
    for lane in (0,1):
        cases={}
        for label,v in modes.items():
            index=0 if label=='H0_H0' else 1 if label=='H1_H1' else lane
            code=f'local h:2 = {value}:2;' if index==0 else f'local shifted:4 = {value} >> 16; local h:2 = shifted:2;'
            cases[(v,) if swizzle in b.values else ()]=code+' export h;'
        lanes.append(b.select_code((swizzle,) if swizzle in b.values else (),cases))
    return lanes


def half_arithmetic(b,fma,check):
    names=half_shape(b,fma)
    is_f32=not fma and b.k.name.startswith('hadd2_F32')
    if check:return bool(names)
    if is_f32:
        require(b,'ofmt',['F32'])
        flush=enum_cases(b,'ftz',['noftz','FTZ'])
        saturate=enum_cases(b,'sat',['nosat','SAT'])
        cswz='iswzB' if 'iswzB' in b.values else 'iswzB_as_C'
        inputs=[half_inputs(b,n,s) for n,s in zip(names,('iswzA',cswz))]
        lhs,rhs=(lane[0] for lane in inputs)
        wide_lhs=b.expression(8,f'float2float({lhs})').symbol
        wide_rhs=b.expression(8,f'float2float({rhs})').symbol
        cases={}
        for (flush_label,flush_value),(sat_label,sat_value) in product(flush.items(),saturate.items()):
            a,c=lhs,rhs
            if flush_label=='FTZ' and not fpops.readable(b):
                a=fpops.flush(a,2);c=fpops.flush(c,2)
                wa=b.expression(8,f'float2float({a})').symbol
                wc=b.expression(8,f'float2float({c})').symbol
            else:wa,wc=wide_lhs,wide_rhs
            if fpops.readable(b):
                code=f'local r:2 = {a} f+ {c};'
            else:
                code=f'local r:2 = float2float({wa} f+ {wc});'
            if flush_label=='FTZ' and not fpops.readable(b):code+=f' r = {fpops.flush("r",2)};'
            if sat_label=='SAT':code+=' '+fpops.saturate(2)
            code+=' local out:4 = float2float(r); export out;'
            cases[(flush_value,sat_value)]=(code)
        table=b.select_code(('ftz','sat'),cases)
        b.body.append(f'{output(b).symbol} = {table};')
        return
    require(b,'ofmt',['F16_V2'])
    if fpops.readable(b):
        for name in ('ftz','fmz'):enum_cases(b,name,FLUSH_LABELS)
    else:require(b,'ftz',['noftz']);require(b,'fmz',['nofmz'])
    satname='satrelu' if 'satrelu' in b.values else 'sat'
    saturate=enum_cases(b,satname,['nosat','SAT'])
    bswz='iswzB' if 'iswzB' in b.values else 'iswzC_as_B'
    cswz='iswzC' if 'iswzC' in b.values else 'iswzB_as_C'
    swizzles=['iswzA',bswz,cswz] if fma else ['iswzA','iswzB' if 'iswzB' in b.values else 'iswzB_as_C']
    inputs=[half_inputs(b,n,s) for n,s in zip(names,swizzles)]
    results=[]
    op='fma' if fma else 'add'
    for lane in (0,1):
        if fpops.readable(b):core=fpops.arithmetic(op,[v[lane] for v in inputs],2,exact=False)
        else:
            # Binary128 holds the exact finite binary16 product AND sum, even at
            # opposite exponent extremes. Narrowing rounds only once to binary16.
            width=16 if fma else 8
            values=[b.expression(width,f'float2float({v[lane]})').symbol for v in inputs]
            core=f'local r:2 = float2float({values[0]} f* {values[1]} f+ {values[2]});' if fma else f'local r:2 = float2float({values[0]} f+ {values[1]});'
        cases={}
        for label,v in saturate.items():
            code=core+' '+(fpops.saturate(2) if label=='SAT' else '')+'export r;'
            cases[(v,) if satname in b.values else ()]=code
        results.append(b.local(2,b.select_code((satname,) if satname in b.values else (),cases)).symbol)
    b.body.append(f'{output(b).symbol} = zext({results[0]}) | (zext({results[1]}) << 16);')


def emit_HADD2(b,check=False):return half_arithmetic(b,False,check)


def emit_HFMA2(b,check=False):return half_arithmetic(b,True,check)


def emit_HMUL2(b,check=False):
    names=operands(b,'Ra/Sa','Rb/Sb/URb')
    if check:
        return (all(names) and
                output(b) is not None and output(b).size==4 and
                all((b.values[n].size in (2,8) and b.values[n+'1'].size==b.values[n].size)
                    if n+'1' in b.values else b.values[n].size==4 for n in names))
    require(b,'ofmt',['F16_V2'])
    fmz_modes=enum_cases(b,'fmz',['nofmz','nofmz_hfma2','FMZ','FTZ'])
    sat_modes=enum_cases(b,'sat',['nosat','SAT'])
    a,c=[half_inputs(b,n,s) for n,s in zip(names,('iswzA','iswzB'))]

    def flush_input(value,label):
        if fpops.readable(b) or label.startswith('nofmz'):return value
        exponent=f'(({value}) & 0x7c00) != 0'
        if label=='FMZ':return f'({value}) * zext({exponent})'
        return f'(({value}) & 0x8000) | ((({value}) & 0x7fff) * zext({exponent}))'

    def flush_result():
        return 'r = (r & 0x8000) | ((r & 0x7fff) * zext((r & 0x7c00) != 0));'

    results=[]
    for lane in (0,1):
        av=b.select('fmz',{value:flush_input(a[lane],label) for label,value in fmz_modes.items()},2)
        cv=b.select('fmz',{value:flush_input(c[lane],label) for label,value in fmz_modes.items()},2)
        core=fpops.arithmetic('mul',[av,cv],2,exact=not fpops.readable(b))
        cases={}
        for fmz_label,fmz_value in fmz_modes.items():
            for sat_label,sat_value in sat_modes.items():
                code=core+' '
                if sat_label=='SAT':code+=fpops.saturate(2)+' '
                if not fpops.readable(b) and fmz_label in ('FMZ','FTZ'):
                    code+=flush_result()+' '
                if not fpops.readable(b) and fmz_label=='FMZ':
                    # HMUL2.FMZ turns any zero/subnormal source into a
                    # canonical +0 result, including 0*Inf (which would be
                    # NaN under ordinary IEEE multiplication).
                    zero_source=f'((({a[lane]}) & 0x7c00) == 0) || ((({c[lane]}) & 0x7c00) == 0)'
                    code+=f'if ({zero_source}) goto <fmz_zero{lane}>; goto <fmz_done{lane}>; <fmz_zero{lane}> r = 0; <fmz_done{lane}> '
                key=tuple(v for name,v in (('fmz',fmz_value),('sat',sat_value)) if name in b.values)
                cases[key]=code+'export r;'
        selected=b.select_code(tuple(n for n in ('fmz','sat') if n in b.values),cases)
        results.append(b.local(2,selected).symbol)
    b.body.append(f'{output(b).symbol} = zext({results[0]}) | (zext({results[1]}) << 16);')


def emit_HMNMX2(b,check=False):
    """Packed 16-bit float min/max, including MD-declared scalar selectors."""
    names=operands(b,'Ra/Sa','Rb/Sb/URb')
    dst=output(b)
    source_predicates=set(b.outputs)=={'Rd','Pu','Pv'}
    if check:
        def packed_source(name):
            value=b.values.get(name)
            if value is None:return False
            paired=b.values.get(name+'1')
            return ((value.size in (2,8) and paired is not None and paired.size==value.size) or
                    (value.size==4 and paired is None))
        return (all(names) and dst is not None and dst.size==4 and
                (set(b.outputs)=={'Rd'} or
                 (set(b.outputs)=={'Rd','Pu','Pv'} and 'isA' in b.values)) and
                all(packed_source(n) for n in names) and
                all(n in b.values for n in ('ofmt','ftz','nan','Pp')))
    formats=b.g.arch.enums[b.k.operand_types['ofmt'].type]
    format_labels=[name for name in ('F16_V2','BF16_V2','E6M9_V2') if name in formats]
    require(b,'ofmt',format_labels)
    f16_value=formats['F16_V2'];bf16_value=formats.get('BF16_V2')
    e6m9_value=formats.get('E6M9_V2')
    ftz_modes=enum_cases(b,'ftz',['noftz','FTZ'])
    nan_modes=enum_cases(b,'nan',['nonan','NAN'])
    xor_modes=enum_cases(b,'xorsign',['noxorsign','XORSIGN'])
    # PT selects min; its negated form selects max. XORSIGN is defined with
    # absolute source values, and the raw signs are captured before those flags.
    abs_attrs=[n+'@absolute' for n in names if n+'@absolute' in b.values]
    selector_names=['ofmt','ftz']
    if 'xorsign' in b.values:selector_names += ['xorsign',*abs_attrs]
    combinations=[]
    fmt_options=[('F16_V2',f16_value),
        *([('BF16_V2',bf16_value)] if bf16_value is not None else []),
        *([('E6M9_V2',e6m9_value)] if e6m9_value is not None else [])]
    noftz=next((value for label,value in ftz_modes.items() if label=='noftz'),None)
    ftz=next((value for label,value in ftz_modes.items() if label=='FTZ'),None)
    for fmt_label,fmt_value in fmt_options:
        for ftz_label,ftz_value in ftz_modes.items():
            # The MD disallows FTZ with BF16/E8M7 and E6M9 encodings.
            if fmt_label in ('BF16_V2','E6M9_V2') and ftz_label!='noftz':continue
            xor_options=xor_modes.items() if 'xorsign' in b.values else [(None,None)]
            for xor_label,xor_value in xor_options:
                for abs_values in product((0,1),repeat=len(abs_attrs)):
                    # For two explicitly modifiable inputs, hardware requires
                    # both abs flags with XORSIGN. RIR's immediate has no abs
                    # bit, so its sign and comparison value remain encoded.
                    if xor_label=='XORSIGN' and len(abs_attrs)==2 and not all(abs_values):continue
                    values=(fmt_value,ftz_value)
                    if 'xorsign' in b.values:values += (xor_value,*abs_values)
                    combinations.append(values)
    if source_predicates:
        require(b,'isA',['IS_A'])
        # The patent describes source indicators but not their interaction
        # with the canonical NAN selector. Keep that selector on the fallback.
        require(b,'nan',['nonan'])
    b.allow_combinations(selector_names,combinations)
    bswz='iswzB' if 'iswzB' in b.values else 'iswzB_as_C'
    inputs=[half_inputs(b,n,s) for n,s in zip(names,('iswzA',bswz))]
    raw=[]
    for name,swizzle in zip(names,('iswzA',bswz)):
        if name+'1' in b.values:
            raw.append([b.expression(2,f'{b.values[n].symbol}:2').symbol
                        for n in (name+'1',name)])
            continue
        packed=b.values[name].symbol
        modes=enum_cases(b,swizzle,['H1_H0','H0_H0','H1_H1'])
        lanes=[]
        for lane in (0,1):
            val=b.local(2,f'({packed} >> {16*lane})') if lane else b.local(2,packed)
            cases={}
            for label,encoded in modes.items():
                source_lane=0 if label=='H0_H0' else 1 if label=='H1_H1' else lane
                code=(f'local h:2 = {packed}:2;' if source_lane==0 else
                      f'local shifted:4 = {packed} >> 16; local h:2 = shifted:2;')
                cases[(encoded,) if swizzle in b.values else ()]=code+' export h;'
            lanes.append(b.select_code((swizzle,) if swizzle in b.values else (),cases))
        raw.append(lanes)
    predicate=b.flag('Pp')
    results=[]
    source_a_flags=[]
    for lane in (0,1):
        avname=f'av{lane}';cvname=f'cv{lane}';afname=f'af{lane}';cfname=f'cf{lane}'
        a,c=(values[lane] for values in inputs)
        ar,cr=(values[lane] for values in raw)
        # The bit-level tests preserve NaN payloads in ordinary mode and make
        # the .NAN canonical result explicit. The decoded predicate chooses
        # min when true and max when false (confirmed by SM89 min/max probes).
        bf_nan_a=f'(({ar} & 0x7f80) == 0x7f80 && ({ar} & 0x007f) != 0)'
        bf_nan_c=f'(({cr} & 0x7f80) == 0x7f80 && ({cr} & 0x007f) != 0)'
        e6_nan_a=f'(({ar} & 0x7e00) == 0x7e00 && ({ar} & 0x01ff) != 0)'
        e6_nan_c=f'(({cr} & 0x7e00) == 0x7e00 && ({cr} & 0x01ff) != 0)'
        h_nan_a=f'(({ar} & 0x7c00) == 0x7c00 && ({ar} & 0x03ff) != 0)'
        h_nan_c=f'(({cr} & 0x7c00) == 0x7c00 && ({cr} & 0x03ff) != 0)'
        fmt=b.values['ofmt'].symbol
        if bf16_value is None and e6m9_value is None:
            is_nan_a=h_nan_a;is_nan_c=h_nan_c;conversion_lines=[]
        else:
            nan_a=[];nan_c=[];non_bf=[]
            if bf16_value is not None:
                nan_a.append(f'({fmt} == {bf16_value}:4 && {bf_nan_a})')
                nan_c.append(f'({fmt} == {bf16_value}:4 && {bf_nan_c})')
                non_bf.append(f'{fmt} != {bf16_value}:4')
            if e6m9_value is not None:
                nan_a.append(f'({fmt} == {e6m9_value}:4 && {e6_nan_a})')
                nan_c.append(f'({fmt} == {e6m9_value}:4 && {e6_nan_c})')
                non_bf.append(f'{fmt} != {e6m9_value}:4')
            nan_a.append(f'(({" && ".join(non_bf)}) && {h_nan_a})')
            nan_c.append(f'(({" && ".join(non_bf)}) && {h_nan_c})')
            is_nan_a='('+' || '.join(nan_a)+')';is_nan_c='('+' || '.join(nan_c)+')'
            conversion_lines=[]
            if bf16_value is not None:
                conversion_lines=[f'if ({fmt} == {bf16_value}:4) goto <hmn_bf_cmp>;',
                    'goto <hmn_cmp_ready>;', '<hmn_bf_cmp>',
                    f'local hmn_bf_a:4 = zext({avname}); local hmn_bf_c:4 = zext({cvname});',
                    'hmn_bf_a = hmn_bf_a << 16; hmn_bf_c = hmn_bf_c << 16;',
                    f'{afname} = float2float(hmn_bf_a); {cfname} = float2float(hmn_bf_c);',
                    '<hmn_cmp_ready>']
        result=b.local(2,'0')
        source_a=b.local(1,'0') if source_predicates else None
        code=[f'local {avname}:2 = {a}; local {cvname}:2 = {c};',
              f'if ({is_nan_a} || {is_nan_c}) goto <hmn_nan>;',
              'goto <hmn_compare>;', '<hmn_nan>',
              f'if ({b.values["nan"].symbol} != {b.g.arch.enums[b.k.operand_types["nan"].type]["NAN"]}:4) goto <hmn_nan_default>;',
              f'{result.symbol} = 0x7fff:2; goto <hmn_done>;',
              '<hmn_nan_default>',
              f'if ({is_nan_a} && !{is_nan_c}) goto <hmn_take_c>;',
              f'if ({is_nan_c} && !{is_nan_a}) goto <hmn_take_a>;',
              f'if ({is_nan_a} && {is_nan_c}) goto <hmn_both_nan>;',
              f'{result.symbol} = {ar}; goto <hmn_done>;',
              '<hmn_both_nan>',
              # The default both-NaN value path canonicalizes the result. This
              # A flag follows the modeled comparison path; GPU evidence is open.
              *([f'{source_a.symbol} = 1:1;'] if source_predicates else []),
              f'{result.symbol} = 0x7fff:2; goto <hmn_done>;',
              '<hmn_compare>',
              f'if ({b.values["ftz"].symbol} == {b.g.arch.enums[b.k.operand_types["ftz"].type].get("FTZ",-1)}:4) goto <hmn_ftz_cmp>;',
              'goto <hmn_cmp_values>;', '<hmn_ftz_cmp>',
              f'{avname} = ({avname} & 0x8000) | (({avname} & 0x7fff) * zext(({avname} & 0x7c00) != 0));',
              f'{cvname} = ({cvname} & 0x8000) | (({cvname} & 0x7fff) * zext(({cvname} & 0x7c00) != 0));',
              '<hmn_cmp_values>',
              f'if (({avname} & 0x7fff) != 0 || ({cvname} & 0x7fff) != 0) goto <hmn_ordered>;',
              f'if ({predicate} != 0) goto <hmn_min_zero>;',
              f'{result.symbol} = {avname} & {cvname};',
              *([f'{source_a.symbol} = ({avname} == {result.symbol});'] if source_predicates else []),
              'goto <hmn_done>;', '<hmn_min_zero>',
              f'{result.symbol} = {avname} | {cvname};',
              *([f'{source_a.symbol} = ({avname} == {result.symbol});'] if source_predicates else []),
              'goto <hmn_done>;', '<hmn_ordered>',
              f'local {afname}:4 = float2float({avname}); local {cfname}:4 = float2float({cvname});',
              *conversion_lines,
              *([f'if ({fmt} == {e6m9_value}:4) goto <hmn_e6_order>;'] if e6m9_value is not None else []),
              f'if (({afname} f< {cfname}) != ({predicate} != 0)) goto <hmn_take_c>;',
              *(["goto <hmn_take_a>;", '<hmn_e6_order>',
                 'local hmn_e6_less:1 = 0;',
                 f'if ((({avname} ^ {cvname}) & 0x8000) != 0) goto <hmn_e6_sign_diff>;',
                 f'if (({avname} & 0x8000) != 0) goto <hmn_e6_both_negative>;',
                 f'hmn_e6_less = ({avname} & 0x7fff) < ({cvname} & 0x7fff); goto <hmn_e6_order_done>;',
                 '<hmn_e6_both_negative>',
                 f'hmn_e6_less = ({avname} & 0x7fff) > ({cvname} & 0x7fff); goto <hmn_e6_order_done>;',
                 '<hmn_e6_sign_diff>',f'hmn_e6_less = ({avname} & 0x8000) != 0;',
                 '<hmn_e6_order_done>',
                 f'if ((hmn_e6_less != 0) != ({predicate} != 0)) goto <hmn_take_c>;'] if e6m9_value is not None else []),
              '<hmn_take_a>',*([f'{source_a.symbol} = 1:1;'] if source_predicates else []),
              f'{result.symbol} = {avname}; goto <hmn_done>;',
              '<hmn_take_c>',*([f'{source_a.symbol} = 0:1;'] if source_predicates else []),
              f'{result.symbol} = {cvname};', '<hmn_done>']
        # FTZ on inputs is applied before NaN/selection; flush the selected
        # result as required by PTX's f16x2 min/max definition.
        if 'xorsign' in b.values:
            xor=b.g.arch.enums[b.k.operand_types['xorsign'].type].get('XORSIGN')
            sign=f'(({ar} ^ {cr}) & 0x8000)'
            nan_mode=b.values['nan'].symbol
            nan_value=b.g.arch.enums[b.k.operand_types['nan'].type].get('NAN')
            code += [f'if ({b.values["xorsign"].symbol} != {xor}:4) goto <hmn_xor_done>;',
                     f'if ({is_nan_a} && {is_nan_c}) goto <hmn_xor_done>;',
                     f'if ({nan_mode} == {nan_value}:4 && ({is_nan_a} || {is_nan_c})) goto <hmn_xor_done>;',
                     f'{result.symbol} = ({result.symbol} & 0x7fff) | {sign};','<hmn_xor_done>']
        if 'ftz' in b.values:
            flush=b.g.arch.enums[b.k.operand_types['ftz'].type].get('FTZ')
            code += [f'if ({b.values["ftz"].symbol} != {flush}:4) goto <hmn_flush_done>;',
                     f'{result.symbol} = ({result.symbol} & 0x8000) | (({result.symbol} & 0x7fff) * zext(({result.symbol} & 0x7c00) != 0));','<hmn_flush_done>']
        code=[line.replace('hmn_',f'hmn{lane}_') for line in code]
        b.body += code
        results.append(result.symbol)
        if source_predicates:source_a_flags.append(source_a.symbol)
    b.body.append(f'{dst.symbol} = zext({results[0]}) | (zext({results[1]}) << 16);')
    if source_predicates:
        b.body += [f'{output(b,"Pu").symbol} = {source_a_flags[0]};',
                   f'{output(b,"Pv").symbol} = {source_a_flags[1]};']


def emit_HFMA2_MMA(b,check=False):
    if check:return all(n in b.values for n in ('Ra','Rb','Sc','Sc1')) and output(b) is not None and output(b).size==4
    # Establish only the constant move used by the compiler. Other MMA forms
    # need separate hardware evidence rather than assuming ordinary fused math.
    require(b,'fmz',['nofmz']);require(b,'satrelu',['nosat']);require(b,'sat',['nosat'])
    for name,negate in [('Ra',1),('Rb',0)]:
        b.allow(name,lambda v:v==255)
        key=name+'@negate'
        if key in b.values:b.allow(key,lambda v,n=negate:v==n)
        elif negate:b.reject()
    # The SM89 zero-source MMA form preserves finite bits and signed zero, but
    # canonicalizes either signaling/quiet NaN to positive 0x7fff per half.
    # Resolve this from encoded immediates so the move stays one p-code COPY.
    def canonicalize(expr):
        # Constructor actions support arithmetic/bit operations, not comparisons.
        # The additions carry only for an all-one exponent / nonzero mantissa.
        isnan=f'((((({expr}) & 0x7c00) + 0x400) >> 15) * (((({expr}) & 0x3ff) + 0x3ff) >> 10))'
        return f'({expr}) * (1 - ({isnan})) + 0x7fff * ({isnan})'
    from sass.gen_sleigh import pattern,pieces
    refs=[];lanes=[]
    for name in ('Sc1','Sc'):
        token,low,high,width,=pieces(b.g.source(b.k,name)[1])[0]
        field=b.g.field(token,low,high,'_sem');refs.append((token,field));lanes.append(canonicalize(field))
    expr=f'({lanes[0]}) | (({lanes[1]}) << 16)'
    table=b.g.subtable('halfmove',[('t',pattern([],refs),f' [ t = inst_start * 0 + ({expr}); ]','export *[const]:4 t;')])
    b.refs.append(('sub',table));b.body.append(f'{output(b).symbol} = {table};')

def emit_FADD(b,check=False):
    names=float_shape(b,'Ra/Sa','Rc/Sc/URc')
    if check:return bool(names)
    mode,flush,xs=float_inputs(b,names)
    float_result(b,mode,flush,lambda rnd,_:arithmetic(b,'add',xs,4,rnd))

def emit_FMUL(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb')
    if check:return bool(names)
    mode,flush,xs=float_inputs(b,names)
    scale=enum_cases(b,'scale',list(SCALES))
    float_result(b,mode,flush,lambda rnd,label:arithmetic(b,'mul',xs,4,rnd,SCALES[label] if label else 0),
                 ('scale',scale) if scale!={None:None} else None)

def emit_FFMA(b,check=False):
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb','Rc/Sc/URc')
    if check:return bool(names)
    mode,flush,xs=float_inputs(b,names)
    float_result(b,mode,flush,lambda rnd,_:arithmetic(b,'fma',xs,4,rnd))

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
    _,_,(a,c)=float_inputs(b,names)
    predicate=b.flag(p)
    xorsign_modes=enum_cases(b,'xorsign',['noxorsign','XORSIGN'])
    if fpops.readable(b):
        enum_cases(b,'nan',['nonan','NAN'])
        result=b.local(4,c)
        b.body += [f'if (({a} f< {c}) != {predicate}) goto <minmaxdone>;',f'{result.symbol} = {a};',
                   '<minmaxdone>',f'{output(b).symbol} = {result.symbol};']
        return
    nan_modes=enum_cases(b,'nan',['nonan','NAN'])
    # Numeric input wins over NaN. Equal signed zeros use OR for min and AND
    # for max; this preserves -0 for min and +0 for max in either source order.
    cases={}
    has_nan_selector='nan' in b.values
    has_xorsign_selector='xorsign' in b.values
    for xormode,xorvalue in xorsign_modes.items():
      for label,value in nan_modes.items():
        if label is None:label='nonan'
        use_xor=xormode=='XORSIGN'
        selected='<fmnmx_select_done>' if use_xor else '<minmaxdone>'
        aa=f'({a} & 0x7fffffff)' if use_xor else a
        cc=f'({c} & 0x7fffffff)' if use_xor else c
        # XORSIGN captures the encoded register signs before SASS's source
        # absolute modifiers (ptxas emits |Ra|/|Rb| for min.xorsign.abs).
        raw_a=b.values[names[0]].symbol;raw_c=b.values[names[1]].symbol
        xor_sign=f'(({raw_a} ^ {raw_c}) & 0x80000000)'
        lines=['local r:4 = '+cc+';']
        if label=='NAN':
            lines += [f'if (nan({aa}) || nan({cc})) goto <nan_result>;']
        else:
            lines += [f'if (!nan({aa})) goto <numeric_a>;',
                      f'if (!nan({cc})) goto {selected};',
                      'r = 0x7fffffff;',f'goto {selected};','<numeric_a>',
                      f'if (nan({cc})) goto <choosea>;']
        lines += [f'if ((({aa} | {cc}) & 0x7fffffff) != 0) goto <compare>;',
                  f'if ({predicate} == 0) goto <maxzero>;',
                  f'r = {aa} | {cc};',f'goto {selected};',
                  '<maxzero>',f'r = {aa} & {cc};',f'goto {selected};',
                  '<compare>',f'if (({aa} f< {cc}) != {predicate}) goto {selected};',
                  *(['<choosea>'] if label!='NAN' else []),f'r = {aa};']
        if label=='NAN':lines += [f'goto {selected};','<nan_result>','r = 0x7fffffff;']
        if use_xor:
            lines += ['<fmnmx_select_done>',f'if (nan(r)) goto <minmaxdone>;',
                      f'r = (r & 0x7fffffff) | {xor_sign};']
        lines += ['<minmaxdone>','export r;']
        key=()
        if has_xorsign_selector:key+=(xorvalue,)
        if has_nan_selector:key+=(value,)
        cases[key]='\n'.join(lines)
    selectors=tuple(n for n,present in (('xorsign',has_xorsign_selector),('nan',has_nan_selector)) if present)
    table=b.select_code(selectors,cases)
    b.body.append(f'{output(b).symbol} = {table};')


def emit_FMNMX3(b,check=False):
    """Three-input scalar float min/max selected by the predicate value."""
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb','Rc/Sc/URc')
    p=operands(b,'Pp')[0]
    if check:return bool(names and p) and set(b.outputs)=={'Rd'}
    _,_,(a,c,d)=float_inputs(b,names)
    pred=b.flag(p)
    nan_modes=enum_cases(b,'nan',['nonan','NAN'])
    if fpops.readable(b):
        first=b.local(4,a);second=b.local(4,'0');third=b.local(4,'0')
        choose_c=f'(({pred} != 0 && {first.symbol} f> {c}) || ({pred} == 0 && {first.symbol} f< {c}))'
        choose_d=f'(({pred} != 0 && {second.symbol} f> {d}) || ({pred} == 0 && {second.symbol} f< {d}))'
        b.body += [f'if {choose_c} goto <take_c>; {second.symbol} = {first.symbol}; goto <first_done>;',
                   '<take_c>',f'{second.symbol} = {c};','<first_done>',
                   f'if {choose_d} goto <take_d>; {third.symbol} = {second.symbol}; goto <second_done>;',
                   '<take_d>',f'{third.symbol} = {d};','<second_done>',
                   f'{output(b).symbol} = {third.symbol};']
        return
    has_nan='nan' in b.values
    cases={}
    for label,value in nan_modes.items():
        first='m1';second='m2'
        lines=['local m1:4 = 0;','local m2:4 = 0;']
        if label=='NAN':
            lines += [f'if (nan({a}) || nan({c}) || nan({d})) goto <fmnmx3_nan>;']
        for left,right,target,tag in ((a,c,first,'ab'),(first,d,second,'abc')):
            # PTX min/max's default NaN rule is numeric-wins. Equal zero
            # operands choose -0 for min and +0 for max, independent of order.
            lines += [f'if (nan({left})) goto <fmnmx3_{tag}_left_nan>;',
                      f'if (nan({right})) goto <fmnmx3_{tag}_right_nan>;',
                      f'if ((({left} | {right}) & 0x7fffffff) == 0) goto <fmnmx3_{tag}_zero>;',
                      f'if (({pred} != 0 && {left} f> {right}) || ({pred} == 0 && {left} f< {right})) goto <fmnmx3_{tag}_right>;',
                      f'{target} = {left}; goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_right>',f'{target} = {right}; goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_left_nan>',f'{target} = {right};',
                      f'if (nan({right})) goto <fmnmx3_{tag}_both_nan>;',
                      f'goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_right_nan>',f'{target} = {left}; goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_both_nan>',f'{target} = {left}; goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_zero>',
                      f'if ({pred} != 0) goto <fmnmx3_{tag}_min_zero>;',
                      f'{target} = {left} & {right}; goto <fmnmx3_{tag}_done>;',
                      f'<fmnmx3_{tag}_min_zero>',
                      f'{target} = {left} | {right};',
                      f'<fmnmx3_{tag}_done>']
        lines += [f'goto <fmnmx3_export>;']
        if label=='NAN':
            lines += ['<fmnmx3_nan>',f'{second} = 0x7fffffff;']
        lines += ['<fmnmx3_export>','local t:4 = '+second+'; export t;']
        cases[(value,) if has_nan else ()]='\n'.join(lines)
    table=b.select_code(('nan',) if has_nan else (),cases)
    b.body.append(f'{output(b).symbol} = {table};')


def emit_FSETP(b,check=False):
    return float_compare(b,4,check)


def emit_F2I(b,check=False):
    """Native SASS F2I.TRUNC.NTZ F32 -> S32 conversion.

    The bit-level conversion avoids a host float cast and matches the measured
    SM89 behavior: truncation toward zero, NaN -> 0, and signed saturation.
    Exact mode accepts only TRUNC; readable mode deliberately ignores rnd.
    """
    src=operands(b,'Rb/Sb/Ra/Sa/URb/URa')[0]
    dst=output(b)
    selectors=('srcfmt','dstfmt','rnd','ntz')
    if check:
        return (src is not None and dst is not None and dst.size==4 and
                b.values[src].size==4 and all(n in b.values for n in selectors) and
                set(b.outputs)=={'Rd'})
    require(b,'srcfmt',['F32']);require(b,'dstfmt',['S32'])
    if not fpops.readable(b):require(b,'rnd',['TRUNC'])
    require(b,'ntz',['NTZ'])
    # Apply the encoded source transforms in FORMAT order before converting.
    bits=b.flag(src,float_value=True)
    sign=b.local(1,f'({bits} >> 31:4) != 0')
    exponent=b.local(4,f'(({bits} >> 23:4) & 0xff:4)')
    fraction=b.local(4,f'({bits} & 0x7fffff:4)')
    magnitude=b.local(8,'0')
    result=b.local(4,'0')
    b.body += [
        f'if ({exponent.symbol} != 0xff) goto <finite>;',
        f'if ({fraction.symbol} != 0) goto <done>;',
        f'if ({sign.symbol} != 0) goto <negative_limit>;',
        f'{result.symbol} = 0x7fffffff; goto <done>;',
        '<negative_limit>',f'{result.symbol} = 0x80000000; goto <done>;',
        '<finite>',
        f'if ({exponent.symbol} == 0) goto <done>;', # subnormals truncate to zero
        f'local e:4 = {exponent.symbol} - 127:4;',
        'if (e s< 0) goto <done>;',
        'if (e s>= 31) goto <limit>;',
        f'local significand:4 = {fraction.symbol} | 0x800000:4;',
        'if (e s< 23:4) goto <right_shift>;',
        f'{magnitude.symbol} = zext(significand) << (e - 23:4); goto <signed_result>;',
        '<right_shift>',f'{magnitude.symbol} = zext(significand) >> (23:4 - e);',
        '<signed_result>',
        f'if ({sign.symbol} == 0) goto <positive_result>;',
        f'if ({magnitude.symbol} > 0x80000000) goto <negative_limit>;',
        f'{result.symbol} = 0:4 - {magnitude.symbol}:4; goto <done>;',
        '<positive_result>',
        f'if ({magnitude.symbol} > 0x7fffffff) goto <positive_limit>;',
        f'{result.symbol} = {magnitude.symbol}:4; goto <done>;',
        '<limit>',f'if ({sign.symbol} != 0) goto <negative_limit>;',
        '<positive_limit>',f'{result.symbol} = 0x7fffffff;',
        '<done>',f'{dst.symbol} = {result.symbol};'
    ]


def emit_F2FP(b,check=False):
    """Packed F2FP conversions with explicit selector and source-form guards."""
    dst=output(b)
    tf32=b.k.name.startswith('f2fp_tf32__')
    if tf32:
        src=operands(b,'Rb/URb/Sb')[0]
        if check:
            return (src is not None and dst is not None and dst.size==4 and
                    b.values[src].size==4 and b.values[src].kind!='runtime-primitive' and
                    all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode','relu')))
        require(b,'dstfmt',['TF32']);require(b,'srcfmt',['F32'])
        require(b,'merge',['PACK_B']);require(b,'rndMode',['RN','RZ'])
        require(b,'relu',['norelu','RELU'])
        relu=b.values['relu'].symbol;relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        bits=b.values[src].symbol;rnd=b.values['rndMode'].symbol
        rz=b.g.arch.enums[b.k.operand_types['rndMode'].type]['RZ']
        result=b.local(4,'0')
        # TF32 retains the sign, exponent and ten high fraction bits.
        b.body += [f'if ({rnd} == {rz}:4) goto <f2fp_tf32_rz>;',
            f'local lsb:4 = ({bits} >> 13) & 1;',
            f'local rounded:4 = {bits} + 0xfff:4 + lsb;',
            f'{result.symbol} = rounded & 0xffffe000:4; goto <f2fp_tf32_done>;',
            '<f2fp_tf32_rz>',f'{result.symbol} = {bits} & 0xffffe000:4;',
            '<f2fp_tf32_done>',
            f'if ({relu} != {relu_value}:4) goto <f2fp_tf32_output>;',
            f'if (({result.symbol} & 0x7f800000:4) == 0x7f800000:4 && ({result.symbol} & 0x007fffff:4) != 0:4) goto <f2fp_tf32_nan>;',
            f'if (({result.symbol} & 0x80000000:4) == 0:4) goto <f2fp_tf32_output>;',
            f'{result.symbol} = 0:4; goto <f2fp_tf32_output>;',
            '<f2fp_tf32_nan>',f'{result.symbol} = 0x7fffffff:4;',
            '<f2fp_tf32_output>',f'{dst.symbol} = {result.symbol};']
        return

    fp4_f32=b.k.name.startswith('f2fp_f32_to_4b_downconvert__')
    if fp4_f32:
        a,source_b,carry=operands(b,'Ra/URa','Rb/URb/Sb','Rc/URc/Sc')
        if check:
            ok=(all((a,source_b,carry)) and dst is not None and dst.size==4 and
                all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive'
                    for n in (a,source_b,carry)) and
                all(n in b.values for n in
                    ('dstfmt','srcfmt','merge','rndMode','relu','satfinite')))
            if not ok:b.unsupported_reason='FP4 F32 downconvert requires two materialized F32 inputs and a materialized merge source'
            return ok
        require(b,'dstfmt',['E2M1','E0M3']);require(b,'srcfmt',['F32'])
        require(b,'merge',['PACK_AB_MERGE_C']);require(b,'rndMode',['RN'])
        require(b,'satfinite',['SATFINITE']);require(b,'relu',['norelu','RELU'])
        enum=b.g.arch.enums[b.k.operand_types['dstfmt'].type]
        e2m1=enum['E2M1'];e0m3=enum['E0M3']
        relu=b.values['relu'].symbol
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        thresholds={
            e2m1:(0x3e800000,0x3f400000,0x3fa00000,0x3fe00000,
                  0x40200000,0x40600000,0x40a00000),
            e0m3:(0x3f000000,0x3fc00000,0x40200000,0x40600000,
                 0x40900000,0x40b00000,0x40d00000)}

        def convert_f32(src,prefix):
            bits=b.flag(src,float_value=True)
            sign=b.local(1,f'({bits} >> 31:4) != 0')
            mag=b.local(4,f'{bits} & 0x7fffffff:4')
            exp=b.local(4,f'({bits} >> 23:4) & 0xff:4')
            frac=b.local(4,f'{bits} & 0x7fffff:4')
            nan=b.local(1,f'({exp.symbol} == 0xff:4) && ({frac.symbol} != 0:4)')
            code=b.local(4,'7:4')
            b.body += [f'if ({exp.symbol} != 0xff:4) goto <{prefix}_finite>;',
                f'goto <{prefix}_special>;',f'<{prefix}_finite>',
                f'if ({b.values["dstfmt"].symbol} == {e2m1}:4) goto <{prefix}_e2m1>;',
                f'goto <{prefix}_e0m3>;',f'<{prefix}_e2m1>']
            for fmt,label in ((e2m1,'e2m1'),(e0m3,'e0m3')):
                if label=='e0m3':b.body.append(f'<{prefix}_e0m3>')
                for idx,threshold in enumerate(thresholds[fmt],start=1):
                    next_label=f'{prefix}_{label}_{idx}'
                    op='>' if idx%2 else '>='
                    b.body += [f'if ({mag.symbol} {op} 0x{threshold:08x}:4) goto <{next_label}>;',
                        f'{code.symbol} = {idx-1}:4; goto <{prefix}_relu>;',
                        f'<{next_label}>']
                b.body.append(f'{code.symbol} = 7:4; goto <{prefix}_relu>;')
            b.body += [f'<{prefix}_special>',f'goto <{prefix}_relu>;',
                f'<{prefix}_relu>',
                f'if ({relu} != {relu_value}:4) goto <{prefix}_sign>;',
                f'if ({sign.symbol} == 0:1) goto <{prefix}_sign>;',
                f'{code.symbol} = 0:4; goto <{prefix}_finish>;',
                f'<{prefix}_sign>',
                f'if ({sign.symbol} == 0:1) goto <{prefix}_finish>;',
                f'if ({nan.symbol} != 0:1) goto <{prefix}_finish>;',
                f'{code.symbol} = {code.symbol} | 8:4;',f'<{prefix}_finish>']
            return code

        av=convert_f32(a,'f2fp4_f32_a')
        bv=convert_f32(source_b,'f2fp4_f32_b')
        cv=b.values[carry].symbol
        b.body.append(f'{dst.symbol} = ({cv} & 0xffffff00:4) | ({av.symbol} << 4:4) | {bv.symbol};')
        return

    fp4_down=b.k.name.startswith('f2fp_f16_to_4b_downconvert__')
    if fp4_down:
        src_b=operands(b,'Rb/URb/Sb')[0]
        src_c=operands(b,'Rc/URc/Sc')[0]
        if check:
            ok=(src_b is not None and src_c is not None and dst is not None and
                dst.size==4 and all(b.values[n].size==4 and
                    b.values[n].kind!='runtime-primitive' for n in (src_b,src_c)) and
                all(n in b.values for n in
                    ('dstfmt','srcfmt','merge','rndMode','relu','satfinite')))
            if not ok:b.unsupported_reason='FP4 downconvert requires two materialized 32-bit sources'
            return ok
        require(b,'dstfmt',['E2M1','E0M3']);require(b,'srcfmt',['F16','BF16'])
        require(b,'merge',['UNPACK_B_MERGE_C']);require(b,'rndMode',['RN'])
        require(b,'satfinite',['SATFINITE']);require(b,'relu',['norelu','RELU'])
        sf=b.flag('srcfmt');relu=b.flag('relu');df=b.flag('dstfmt')
        f16=b.g.arch.enums[b.k.operand_types['srcfmt'].type]['F16']
        e2m1=b.g.arch.enums[b.k.operand_types['dstfmt'].type]['E2M1']
        e0m3=b.g.arch.enums[b.k.operand_types['dstfmt'].type]['E0M3']
        f16_thresholds=(0x3400,0x3a00,0x3d00,0x3f00,0x4100,0x4300,0x4500,0x7c00)
        bf16_thresholds=(0x3e80,0x3f40,0x3fa0,0x3fe0,0x4020,0x4060,0x40a0,0x7f80)
        e0m3_f16_thresholds=(0x3800,0x3e00,0x4100,0x4300,0x4480,0x4580,0x4680,0x7c00)
        e0m3_bf16_thresholds=(0x3f00,0x3fc0,0x4020,0x4060,0x4090,0x40b0,0x40d0,0x7f80)

        def convert_half(src,half_lane,prefix):
            raw=b.local(4,f'({src} >> {half_lane*16}:4) & 0xffff:4')
            sign=b.local(4,f'({raw.symbol} >> 12:4) & 8:4')
            mag=b.local(4,f'{raw.symbol} & 0x7fff:4')
            packed=b.local(4,'0')
            done=f'{prefix}_done'
            b.body += [f'if ({df} == {e0m3}:4) goto <{prefix}_e0m3>;',
                f'goto <{prefix}_e2m1>;',f'<{prefix}_e0m3>',
                f'if ({sf} == {f16}:4) goto <{prefix}_e0m3_f16>;',
                f'goto <{prefix}_e0m3_bf16>;']
            for label,thresholds in (('bf16',e0m3_bf16_thresholds),
                                     ('f16',e0m3_f16_thresholds)):
                b.body.append(f'<{prefix}_e0m3_{label}>')
                inf=thresholds[-1]
                b.body += [f'if ({mag.symbol} > {inf}:4) goto <{prefix}_nan>;',
                    f'if ({mag.symbol} >= {inf}:4) goto <{prefix}_overflow>;']
                for idx,(threshold,operator) in enumerate(zip(
                        thresholds[:7],('>','>=','>','>=','>','>=','>')),start=1):
                    next_label=f'{prefix}_e0m3_{label}_m{idx}'
                    b.body += [f'if ({mag.symbol} {operator} {threshold}:4) goto <{next_label}>;',
                        f'{packed.symbol} = ({sign.symbol} | {idx-1}:4); goto <{done}>;',
                        f'<{next_label}>']
                b.body += [f'{packed.symbol} = ({sign.symbol} | 7:4);',
                    f'goto <{done}>;']
            b.body += [f'goto <{done}>;',f'<{prefix}_e2m1>',
                f'if ({sf} == {f16}:4) goto <{prefix}_f16>;',
                f'goto <{prefix}_bf16>;']
            for fmt,label,thresholds in (
                    ('bf16','bf16',bf16_thresholds),('f16','f16',f16_thresholds)):
                b.body.append(f'<{prefix}_{label}>')
                inf=thresholds[-1]
                b.body += [f'if ({mag.symbol} > {inf}:4) goto <{prefix}_nan>;',
                    f'if ({mag.symbol} >= {inf}:4) goto <{prefix}_overflow>;']
                # Round to nearest even between E2M1 values 0,.5,1,1.5,2,3,4,6.
                # The comparison polarity at each midpoint selects the even
                # target encoding on exact ties.
                for idx,(threshold,operator) in enumerate(zip(
                        thresholds[:7],('>','>=','>','>=','>','>=','>')),start=1):
                    next_label=f'{prefix}_{label}_m{idx}'
                    b.body += [f'if ({mag.symbol} {operator} {threshold}:4) goto <{next_label}>;',
                        f'{packed.symbol} = ({sign.symbol} | {idx-1}:4); goto <{done}>;',
                        f'<{next_label}>']
                b.body += [f'{packed.symbol} = ({sign.symbol} | 7:4);',
                    f'goto <{done}>;']
            b.body += [f'<{prefix}_overflow>',f'{packed.symbol} = ({sign.symbol} | 7:4);',
                f'goto <{done}>;',f'<{prefix}_nan>',f'{packed.symbol} = 7:4;',
                f'goto <{done}>;',f'<{done}>',
                f'if ({relu} == {b.g.arch.enums[b.k.operand_types["relu"].type]["RELU"]}:4 && ({packed.symbol} & 8:4) != 0:4) goto <{prefix}_relu>;',
                f'goto <{prefix}_keep>;',f'<{prefix}_relu>',
                f'{packed.symbol} = 0:4;',f'<{prefix}_keep>']
            return packed

        packed=[]
        for operand,src in enumerate((src_b,src_c)):
            for half_lane in range(2):
                packed.append(convert_half(b.flag(src),half_lane,
                    f'f2fp4_{operand}_{half_lane}'))
        b.body.append(f'{dst.symbol} = ({packed[3].symbol} << 12:4) | ({packed[2].symbol} << 8:4) | ({packed[1].symbol} << 4:4) | {packed[0].symbol};')
        return

    unpack_e8=b.k.name.startswith('f2fp_E8_upconvert__')
    if unpack_e8:
        src=operands(b,'Rb/URb/Sb')[0]
        if check:
            return (src is not None and dst is not None and dst.size==4 and
                b.values[src].size==4 and b.values[src].kind!='runtime-primitive' and
                all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode','extract_B')))
        require(b,'dstfmt',['BF16']);require(b,'srcfmt',['E8'])
        require(b,'merge',['UNPACK_B']);require(b,'rndMode',['RN'])
        require(b,'extract_B',['H0','H1'])
        source=b.flag(src);extract=b.flag('extract_B')
        h1=b.g.arch.enums[b.k.operand_types['extract_B'].type]['H1']
        selected=b.local(4,'0')
        b.body += [f'if ({extract} == {h1}:4) goto <f2fp_e8_h1>;',
            f'{selected.symbol} = {source} & 0xffff:4; goto <f2fp_e8_decode>;',
            '<f2fp_e8_h1>',
            f'{selected.symbol} = ({source} >> 16:4) & 0xffff:4;',
            '<f2fp_e8_decode>']
        lanes=[]
        for lane in range(2):
            byte=b.local(4,f'({selected.symbol} >> {lane*8}:4) & 0xff:4')
            value=b.local(4,f'{byte.symbol} << 7:4')
            b.body += [f'if ({byte.symbol} != 0xff:4) goto <f2fp_e8_not_nan_{lane}>;',
                f'{value.symbol} = 0x7fff:4; goto <f2fp_e8_done_{lane}>;',
                f'<f2fp_e8_not_nan_{lane}>',
                f'if ({byte.symbol} != 0:4) goto <f2fp_e8_done_{lane}>;',
                f'{value.symbol} = 0x40:4;',f'<f2fp_e8_done_{lane}>']
            lanes.append(value)
        b.body.append(f'{dst.symbol} = (({lanes[1].symbol} & 0xffff:4) << 16:4) | ({lanes[0].symbol} & 0xffff:4);')
        return

    unpack4_scale=b.k.name.startswith('f2fp_4b_upconvert_scale__')
    if unpack4_scale:
        src=operands(b,'Rb/URb/Sb')[0]
        scale_src=operands(b,'Rc/URc/Sc')[0]
        if check:
            return (src is not None and scale_src is not None and dst is not None and
                dst.size==4 and all(b.values[n].size==4 and
                    b.values[n].kind!='runtime-primitive' for n in (src,scale_src)) and
                all(n in b.values for n in
                    ('dstfmt','srcfmt','merge','rndMode','relu','satfinite','selB')))
        require(b,'dstfmt',['BF16']);require(b,'srcfmt',['E2M1','E0M3'])
        require(b,'merge',['UNPACK_B']);require(b,'rndMode',['RN'])
        require(b,'relu',['norelu','RELU']);require(b,'satfinite',['SATFINITE'])
        require(b,'selB',['B0','B1','B2','B3'])
        source=b.flag(src);scale_word=b.flag(scale_src)
        selector=b.flag('selB');relu=b.flag('relu');srcfmt=b.flag('srcfmt')
        formats=b.g.arch.enums[b.k.operand_types['srcfmt'].type]
        e2m1=formats['E2M1'];e0m3=formats['E0M3']
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        packed=b.local(4,f'({source} >> ({selector} * 8:4)) & 0xff:4')
        outputs=[]
        for lane in range(2):
            prefix=f'f2fp4scale_{lane}'
            nibble=b.local(4,f'({packed.symbol} >> {lane*4}:4) & 0xf:4')
            value=b.local(4,'0')
            scale_byte=b.local(4,f'({scale_word} >> {lane*8}:4) & 0xff:4')
            scale_bits=b.local(4,'0')
            scaled=b.local(4,'0')
            result=b.local(4,'0')
            b.body += [f'if ({srcfmt} == {e2m1}:4) goto <{prefix}_e2m1>;',
                f'local {prefix}_mag:4 = {nibble.symbol} & 7:4;']
            for magnitude,bits in enumerate((0,0x3f800000,0x40000000,0x40400000,
                                              0x40800000,0x40a00000,0x40c00000,0x40e00000)):
                b.body += [f'if ({prefix}_mag != {magnitude}:4) goto <{prefix}_e0m3_{magnitude}>;',
                    f'{value.symbol} = 0x{bits:08x}:4 + (({nibble.symbol} & 8:4) << 28:4); goto <{prefix}_value_ready>;',
                    f'<{prefix}_e0m3_{magnitude}>']
            b.body += [f'goto <{prefix}_value_ready>;',f'<{prefix}_e2m1>',
                f'local {prefix}_e2m1_mag:4 = {nibble.symbol} & 7:4;']
            for magnitude,bits in enumerate((0,0x3f000000,0x3f800000,0x3fc00000,
                                              0x40000000,0x40400000,0x40800000,0x40c00000)):
                b.body += [f'if ({prefix}_e2m1_mag != {magnitude}:4) goto <{prefix}_e2m1_{magnitude}>;',
                    f'{value.symbol} = 0x{bits:08x}:4 + (({nibble.symbol} & 8:4) << 28:4); goto <{prefix}_value_ready>;',
                    f'<{prefix}_e2m1_{magnitude}>']
            b.body += [f'<{prefix}_value_ready>',
                f'if ({scale_byte.symbol} != 0:4) goto <{prefix}_scale_nonzero>;',
                f'{scale_bits.symbol} = 0x00400000:4; goto <{prefix}_scale_ready>;',
                f'<{prefix}_scale_nonzero>',
                f'if ({scale_byte.symbol} != 0xff:4) goto <{prefix}_scale_finite>;',
                f'{scale_bits.symbol} = 0x7fc00000:4; goto <{prefix}_scale_ready>;',
                f'<{prefix}_scale_finite>',f'{scale_bits.symbol} = {scale_byte.symbol} << 23:4;',
                f'<{prefix}_scale_ready>',
                f'{scaled.symbol} = {value.symbol} f* {scale_bits.symbol};',
                f'if (({scaled.symbol} & 0x7f800000:4) != 0x7f800000:4 || ({scaled.symbol} & 0x007fffff:4) == 0:4) goto <{prefix}_round>;',
                f'{result.symbol} = 0x7fff:4; goto <{prefix}_done>;',
                f'<{prefix}_round>',
                f'{result.symbol} = ({scaled.symbol} + 0x7fff:4 + (({scaled.symbol} >> 16:4) & 1:4)) >> 16:4;',
                f'if ({b.flag("satfinite")} != {b.g.arch.enums[b.k.operand_types["satfinite"].type]["SATFINITE"]}:4) goto <{prefix}_relu>;',
                f'if (({result.symbol} & 0x7fff:4) != 0x7f80:4) goto <{prefix}_relu>;',
                f'{result.symbol} = ({result.symbol} & 0x8000:4) + 0x7f7f:4;',
                f'<{prefix}_relu>',
                f'if ({relu} != {relu_value}:4) goto <{prefix}_done>;',
                f'if (({result.symbol} & 0x8000:4) == 0:4) goto <{prefix}_done>;',
                f'if (({result.symbol} & 0x7f80:4) == 0x7f80:4 && ({result.symbol} & 0x007f:4) != 0:4) goto <{prefix}_done>;',
                f'{result.symbol} = 0:4;',f'<{prefix}_done>']
            outputs.append(result)
        b.body.append(f'{dst.symbol} = (({outputs[1].symbol} & 0xffff:4) << 16:4) + ({outputs[0].symbol} & 0xffff:4);')
        return

    unpack8_scale=b.k.name.startswith('f2fp_8b_upconvert_scale__')
    if unpack8_scale:
        src=operands(b,'Rb/URb/Sb')[0]
        scale_src=operands(b,'Rc/URc/Sc')[0]
        if check:
            return (src is not None and scale_src is not None and dst is not None and
                dst.size==4 and all(b.values[n].size==4 and
                    b.values[n].kind!='runtime-primitive' for n in (src,scale_src)) and
                all(n in b.values for n in
                    ('dstfmt','srcfmt','merge','rndMode','relu','satfinite','extract_B')))
        require(b,'dstfmt',['BF16']);require(b,'merge',['UNPACK_B'])
        require(b,'rndMode',['RN']);require(b,'relu',['norelu','RELU'])
        require(b,'satfinite',['nosatfinite','SATFINITE']);require(b,'extract_B',['H0','H1'])
        require(b,'srcfmt',['E5M2','E4M3','E2M3','E3M2','E3M4','S2_6'])
        source=b.flag(src);scale_word=b.flag(scale_src)
        extract=b.flag('extract_B');relu=b.flag('relu');satfinite=b.flag('satfinite')
        srcfmt=b.flag('srcfmt')
        formats=b.g.arch.enums[b.k.operand_types['srcfmt'].type]
        h1=b.g.arch.enums[b.k.operand_types['extract_B'].type]['H1']
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        satfinite_value=b.g.arch.enums[b.k.operand_types['satfinite'].type]['SATFINITE']
        selected=b.local(4,'0');selected_scale=b.local(4,'0')
        b.body += [f'if ({extract} == {h1}:4) goto <f2fp8scale_h1>;',
            f'{selected.symbol} = {source} & 0xffff:4;',
            f'{selected_scale.symbol} = {scale_word} & 0xffff:4; goto <f2fp8scale_selected>;',
            '<f2fp8scale_h1>',
            f'{selected.symbol} = ({source} >> 16:4) & 0xffff:4;',
            f'{selected_scale.symbol} = ({scale_word} >> 16:4) & 0xffff:4;',
            '<f2fp8scale_selected>']

        def decode_source(byte,prefix):
            value=b.local(4,'0')
            descriptors=(
                ('E5M2',7,5,2,15,31,0),
                ('E4M3',7,4,3,7,15,7),
                ('E3M4',7,3,4,3,7,15),
                ('E2M3',5,2,3,1,None,None),
                ('E3M2',5,3,2,3,None,None),
            )
            dispatch=[];code=[]
            for name,signbit,ebits,mbits,bias,special_exp,special_mant in descriptors:
                if name not in formats:continue
                fmtval=formats[name];label=f'{prefix}_{name.lower()}'
                sign=b.local(4,f'({byte.symbol} & {1<<signbit}:4) << {31-signbit}:4')
                exp=b.local(4,f'({byte.symbol} >> {mbits}:4) & {(1<<ebits)-1}:4')
                mant=b.local(4,f'{byte.symbol} & {(1<<mbits)-1}:4')
                dispatch.append(f'if ({srcfmt} == {fmtval}:4) goto <{label}>;')
                code.append(f'<{label}>')
                if name=='E5M2':
                    code += [f'if ({exp.symbol} != 31:4) goto <{label}_finite>;',
                        f'if ({mant.symbol} != 0:4) goto <{label}_nan>;',
                        f'{value.symbol} = {sign.symbol} | 0x7f800000:4; goto <{prefix}_decoded>;',
                        f'<{label}_nan>',f'{value.symbol} = 0x7fc00000:4; goto <{prefix}_decoded>;',
                        f'<{label}_finite>']
                elif special_exp is not None:
                    code += [f'if ({exp.symbol} != {special_exp}:4) goto <{label}_finite>;',
                        f'if ({mant.symbol} != {special_mant}:4) goto <{label}_finite>;',
                        f'{value.symbol} = 0x7fc00000:4; goto <{prefix}_decoded>;',
                        f'<{label}_finite>']
                code += [
                    f'if ({exp.symbol} != 0:4) goto <{label}_normal>;',
                    f'if ({mant.symbol} == 0:4) goto <{label}_zero>;']
                for top in range(mbits-1,-1,-1):
                    code.append(f'if ({mant.symbol} >= {1<<top}:4) goto <{label}_sub{top}>;')
                for top in range(mbits-1,-1,-1):
                    unbiased=1-bias-mbits+top
                    code += [f'<{label}_sub{top}>',
                        f'{value.symbol} = {sign.symbol} | ({unbiased+127}:4 << 23:4) | '
                        f'(({mant.symbol} - {1<<top}:4) << {23-top}:4);',
                        f'goto <{prefix}_decoded>;']
                code += [f'<{label}_normal>',
                    f'{value.symbol} = {sign.symbol} | '
                    f'(({exp.symbol} - {bias}:4 + 127:4) << 23:4) | '
                    f'({mant.symbol} << {23-mbits}:4);',
                    f'goto <{prefix}_decoded>;',
                    f'<{label}_zero>',f'{value.symbol} = {sign.symbol};',
                    f'goto <{prefix}_decoded>;']

            s2f6=formats.get('S2_6')
            if s2f6 is not None:
                dispatch.append(f'if ({srcfmt} == {s2f6}:4) goto <{prefix}_s2_6>;')
                signed=b.local(4,f'sext({byte.symbol}:1)')
                code += [f'<{prefix}_s2_6>',
                    f'{value.symbol} = int2float({signed.symbol}) f* 0x3c800000:4;',
                    f'goto <{prefix}_decoded>;']
            b.body += [*dispatch,*code,f'<{prefix}_decoded>']
            return value

        outputs=[]
        for lane in range(2):
            prefix=f'f2fp8scale_{lane}'
            byte=b.local(4,f'({selected.symbol} >> {lane*8}:4) & 0xff:4')
            scale_byte=b.local(4,f'({selected_scale.symbol} >> {lane*8}:4) & 0xff:4')
            value=decode_source(byte,prefix)
            scale_bits=b.local(4,'0');scaled=b.local(4,'0');result=b.local(4,'0')
            b.body += [f'if ({scale_byte.symbol} != 0:4) goto <{prefix}_scale_nonzero>;',
                f'{scale_bits.symbol} = 0x00400000:4; goto <{prefix}_scale_ready>;',
                f'<{prefix}_scale_nonzero>',
                f'if ({scale_byte.symbol} != 0xff:4) goto <{prefix}_scale_finite>;',
                f'{scale_bits.symbol} = 0x7fc00000:4; goto <{prefix}_scale_ready>;',
                f'<{prefix}_scale_finite>',
                f'{scale_bits.symbol} = {scale_byte.symbol} << 23:4;',
                f'<{prefix}_scale_ready>',
                f'{scaled.symbol} = {value.symbol} f* {scale_bits.symbol};',
                f'if (({scaled.symbol} & 0x7f800000:4) != 0x7f800000:4 || '
                f'({scaled.symbol} & 0x007fffff:4) == 0:4) goto <{prefix}_round>;',
                f'{result.symbol} = 0x7fff:4; goto <{prefix}_done>;',
                f'<{prefix}_round>',
                f'{result.symbol} = ({scaled.symbol} + 0x7fff:4 + '
                f'(({scaled.symbol} >> 16:4) & 1:4)) >> 16:4;',
                f'if ({satfinite} != {satfinite_value}:4) goto <{prefix}_relu>;',
                f'if (({result.symbol} & 0x7fff:4) != 0x7f80:4) goto <{prefix}_relu>;',
                f'{result.symbol} = ({result.symbol} & 0x8000:4) | 0x7f7f:4;',
                f'<{prefix}_relu>',
                f'if ({relu} != {relu_value}:4) goto <{prefix}_done>;',
                f'if (({result.symbol} & 0x8000:4) == 0:4) goto <{prefix}_done>;',
                f'if (({result.symbol} & 0x7f80:4) == 0x7f80:4 && '
                f'({result.symbol} & 0x007f:4) != 0:4) goto <{prefix}_done>;',
                f'{result.symbol} = 0:4;',f'<{prefix}_done>']
            outputs.append(result)
        b.body.append(f'{dst.symbol} = (({outputs[1].symbol} & 0xffff:4) << 16:4) | '
                      f'({outputs[0].symbol} & 0xffff:4);')
        return

    unpack4=b.k.name.startswith('f2fp_4b_upconvert__')
    if unpack4:
        src=operands(b,'Rb/URb/Sb')[0]
        if check:
            return (src is not None and dst is not None and dst.size==4 and
                b.values[src].size==4 and b.values[src].kind!='runtime-primitive' and
                all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode','relu','selB')))
        require(b,'dstfmt',['F16']);require(b,'srcfmt',['E2M1','E0M3'])
        require(b,'merge',['UNPACK_B']);require(b,'rndMode',['RN'])
        require(b,'relu',['norelu','RELU']);require(b,'selB',['B0','B1','B2','B3'])
        source=b.flag(src);selector=b.flag('selB');relu=b.flag('relu')
        srcfmt=b.flag('srcfmt')
        e2m1=b.g.arch.enums[b.k.operand_types['srcfmt'].type]['E2M1']
        e0m3=b.g.arch.enums[b.k.operand_types['srcfmt'].type]['E0M3']
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        packed=b.local(4,f'({source} >> ({selector} * 8:4)) & 0xff:4')
        results=[]
        for lane in range(2):
            nibble=b.local(4,f'({packed.symbol} >> {lane*4}:4) & 0xf:4')
            sign=b.local(4,f'({nibble.symbol} & 8:4) << 12:4')
            exponent=b.local(4,f'({nibble.symbol} >> 1:4) & 3:4')
            mantissa=b.local(4,f'{nibble.symbol} & 1:4')
            half=b.local(4,'0')
            b.body += [f'if ({srcfmt} == {e2m1}:4) goto <f2fp4_e2m1_{lane}>;',
                f'local e0m3_mag_{lane}:4 = {nibble.symbol} & 7:4;']
            for magnitude,half_bits in enumerate((0,0x3c00,0x4000,0x4200,
                                                   0x4400,0x4500,0x4600,0x4700)):
                b.body += [f'if (e0m3_mag_{lane} != {magnitude}:4) goto <f2fp4_e0m3_{lane}_{magnitude}>;',
                    f'{half.symbol} = {sign.symbol} | 0x{half_bits:04x}:4; goto <f2fp4_relu_{lane}>;',
                    f'<f2fp4_e0m3_{lane}_{magnitude}>']
            b.body += [f'goto <f2fp4_relu_{lane}>;',f'<f2fp4_e2m1_{lane}>',
                f'if ({exponent.symbol} != 0:4) goto <f2fp4_normal_{lane}>;',
                f'if ({mantissa.symbol} == 0:4) goto <f2fp4_zero_{lane}>;',
                f'{half.symbol} = {sign.symbol} | 0x3800:4; goto <f2fp4_relu_{lane}>;',
                f'<f2fp4_zero_{lane}>',f'{half.symbol} = {sign.symbol};',
                f'goto <f2fp4_relu_{lane}>;',f'<f2fp4_normal_{lane}>',
                f'{half.symbol} = {sign.symbol} | (({exponent.symbol} + 14:4) << 10:4) | ({mantissa.symbol} << 9:4);',
                f'<f2fp4_relu_{lane}>',
                f'if ({relu} != {relu_value}:4) goto <f2fp4_keep_{lane}>;',
                f'if (({half.symbol} & 0x8000:4) == 0:4) goto <f2fp4_keep_{lane}>;',
                f'{half.symbol} = 0:4;',f'<f2fp4_keep_{lane}>']
            results.append(half)
        b.body.append(f'{dst.symbol} = (({results[1].symbol} & 0xffff:4) << 16:4) | ({results[0].symbol} & 0xffff:4);')
        return

    unpack=b.k.name.startswith('f2fp_8b_upconvert__')
    if unpack:
        src=operands(b,'Rb/URb/Sb')[0]
        if check:
            return (src is not None and dst is not None and dst.size==4 and
                b.values[src].size==4 and b.values[src].kind!='runtime-primitive' and
                all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode','relu','extract_B')))
        require(b,'dstfmt',['F16'])
        require(b,'merge',['UNPACK_B']);require(b,'rndMode',['RN'])
        require(b,'relu',['norelu','RELU']);require(b,'extract_B',['H0','H1'])
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        relu=b.values['relu'].symbol
        srcfmt=b.values['srcfmt'].symbol
        formats=b.g.arch.enums[b.k.operand_types['srcfmt'].type]
        e4=formats['E4M3'];e5=formats['E5M2']
        fp6_formats=[name for name in ('E2M3','E3M2') if name in formats]
        e3m4=formats.get('E3M4')
        require(b,'srcfmt',['E4M3','E5M2',*fp6_formats,*(['E3M4'] if e3m4 is not None else [])])
        extract=b.values['extract_B'].symbol
        h1=b.g.arch.enums[b.k.operand_types['extract_B'].type]['H1']
        packed=b.values[src].symbol
        selected=b.local(4,'0')
        b.body += [f'if ({extract} == {h1}:4) goto <f2fp_unpack_h1>;',
            f'{selected.symbol} = {packed} & 0xffff:4; goto <f2fp_unpack_decode>;',
            '<f2fp_unpack_h1>',f'{selected.symbol} = ({packed} >> 16:4) & 0xffff:4;',
            '<f2fp_unpack_decode>']

        def unpack_byte(shift,prefix):
            byte=b.local(4,f'({selected.symbol} >> {shift}:4) & 0xff:4')
            sign=b.local(4,f'({byte.symbol} & 0x80:4) << 8:4')
            exp=b.local(4,f'({byte.symbol} >> 3:4) & 0xf:4')
            mant=b.local(4,f'{byte.symbol} & 7:4')
            half_exp=b.local(4,'0');fraction=b.local(4,'0')
            magnitude=b.local(4,'0');result=b.local(4,'0')
            fp6_dispatch=[];fp6_code=[];e3m4_dispatch=[];e3m4_code=[]
            if e3m4 is not None:
                e3_exp=b.local(4,f'({byte.symbol} >> 4:4) & 7:4')
                e3_mant=b.local(4,f'{byte.symbol} & 0xf:4')
                e3m4_dispatch=[f'if ({srcfmt} == {e3m4}:4) goto <{prefix}_e3m4>;']
                e3m4_code=[f'<{prefix}_e3m4>',
                    f'if ({e3_exp.symbol} != 7:4) goto <{prefix}_e3m4_not_nan>;',
                    f'if ({e3_mant.symbol} != 0xf:4) goto <{prefix}_e3m4_not_nan>;',
                    f'{result.symbol} = 0x7fff:4; goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_not_nan>',
                    f'if ({e3_exp.symbol} != 0:4) goto <{prefix}_e3m4_normal>;',
                    f'if ({e3_mant.symbol} == 0:4) goto <{prefix}_e3m4_zero>;',
                    f'if ({e3_mant.symbol} >= 8:4) goto <{prefix}_e3m4_sub8>;',
                    f'if ({e3_mant.symbol} >= 4:4) goto <{prefix}_e3m4_sub4>;',
                    f'if ({e3_mant.symbol} >= 2:4) goto <{prefix}_e3m4_sub2>;',
                    f'{result.symbol} = {sign.symbol} | (9:4 << 10:4); goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_sub2>',
                    f'{result.symbol} = {sign.symbol} | (10:4 << 10:4) | (({e3_mant.symbol} - 2:4) << 9:4); goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_sub4>',
                    f'{result.symbol} = {sign.symbol} | (11:4 << 10:4) | (({e3_mant.symbol} - 4:4) << 8:4); goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_sub8>',
                    f'{result.symbol} = {sign.symbol} | (12:4 << 10:4) | (({e3_mant.symbol} - 8:4) << 7:4); goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_zero>',f'{result.symbol} = {sign.symbol}; goto <{prefix}_finish>;',
                    f'<{prefix}_e3m4_normal>',
                    f'{result.symbol} = {sign.symbol} | (({e3_exp.symbol} + 12:4) << 10:4) | ({e3_mant.symbol} << 6:4);',
                    f'goto <{prefix}_finish>;']
            for name,ebits,mbits,bias in (('E2M3',2,3,1),('E3M2',3,2,3)):
                if name not in fp6_formats:continue
                label=name.lower()
                fields_exp=b.local(4,f'({byte.symbol} >> {mbits}:4) & {(1<<ebits)-1}:4')
                fields_mant=b.local(4,f'{byte.symbol} & {(1<<mbits)-1}:4')
                fields_sign=b.local(4,f'({byte.symbol} & 0x20:4) << 10:4')
                fp6_dispatch += [f'if ({srcfmt} == {formats[name]}:4) goto <{prefix}_{label}>;']
                fp6_code += [f'<{prefix}_{label}>',
                    f'if ({fields_exp.symbol} != 0:4) goto <{prefix}_{label}_normal>;',
                    f'if ({fields_mant.symbol} == 0:4) goto <{prefix}_{label}_zero>;']
                for top in range(mbits-1,-1,-1):
                    fp6_code += [f'if (({fields_mant.symbol} & {1<<top}:4) != 0:4) goto <{prefix}_{label}_sub{top}>;']
                for top in range(mbits-1,-1,-1):
                    half_e=15 + 1 - bias - mbits + top
                    frac_shift=10-top
                    fp6_code += [f'<{prefix}_{label}_sub{top}>',
                        f'{result.symbol} = {fields_sign.symbol} | ({half_e}:4 << 10:4) | '
                        f'(({fields_mant.symbol} & {(1<<top)-1}:4) << {frac_shift}:4);',
                        f'goto <{prefix}_finish>;']
                fp6_code += [f'<{prefix}_{label}_normal>',
                    f'{result.symbol} = {fields_sign.symbol} | '
                    f'(({fields_exp.symbol} - {bias}:4 + 15:4) << 10:4) | '
                    '({} << {}:4);'.format(fields_mant.symbol,10-mbits),
                    f'goto <{prefix}_finish>;',
                    f'<{prefix}_{label}_zero>',
                    f'{result.symbol} = {fields_sign.symbol};',
                    f'goto <{prefix}_finish>;']
            b.body += [*e3m4_dispatch,*fp6_dispatch,
                f'if ({srcfmt} == {e5}:4) goto <{prefix}_e5>;',
                f'if (({exp.symbol} == 0xf:4) && ({mant.symbol} == 7:4)) goto <{prefix}_nan>;',
                f'if ({exp.symbol} != 0:4) goto <{prefix}_normal>;',
                f'if ({mant.symbol} == 0:4) goto <{prefix}_zero>;',
                f'if (({mant.symbol} & 4:4) != 0:4) goto <{prefix}_sub4>;',
                f'if (({mant.symbol} & 2:4) != 0:4) goto <{prefix}_sub2>;',
                f'{half_exp.symbol} = 6:4; goto <{prefix}_pack>;',
                f'<{prefix}_sub4>',f'{half_exp.symbol} = 8:4;',
                f'{fraction.symbol} = ({mant.symbol} & 3:4) << 8:4; goto <{prefix}_pack>;',
                f'<{prefix}_sub2>',f'{half_exp.symbol} = 7:4;',
                f'{fraction.symbol} = ({mant.symbol} & 1:4) << 9:4; goto <{prefix}_pack>;',
                f'<{prefix}_normal>',f'{half_exp.symbol} = {exp.symbol} + 8:4;',
                f'{fraction.symbol} = {mant.symbol} << 7:4; goto <{prefix}_pack>;',
                f'<{prefix}_zero>',f'{magnitude.symbol} = 0:4; goto <{prefix}_done>;',
                f'<{prefix}_nan>',f'{result.symbol} = 0x7fff:4; goto <{prefix}_finish>;',
                f'<{prefix}_pack>',
                f'{magnitude.symbol} = ({half_exp.symbol} << 10:4) | {fraction.symbol};',
                f'goto <{prefix}_done>;',
                f'<{prefix}_e5>',
                f'if ((({byte.symbol} & 0x7c:4) == 0x7c:4) && (({byte.symbol} & 3:4) != 0:4)) goto <{prefix}_nan>;',
                f'{magnitude.symbol} = ({byte.symbol} & 0x7f:4) << 8:4;',
                f'<{prefix}_done>',f'{result.symbol} = {sign.symbol} | {magnitude.symbol};',
                f'goto <{prefix}_finish>;',
                *e3m4_code,*fp6_code,
                f'<{prefix}_finish>',
                f'if ({relu} != {relu_value}:4) goto <{prefix}_relu_done>;',
                f'if (({result.symbol} & 0x8000:4) == 0:4) goto <{prefix}_relu_done>;',
                f'{result.symbol} = 0:4;',f'<{prefix}_relu_done>']
            return result

        low=unpack_byte(0,'f2fp_unpack_lo')
        high=unpack_byte(8,'f2fp_unpack_hi')
        b.body += [f'{dst.symbol} = ({high.symbol} << 16:4) | {low.symbol};']
        return

    fp8_f32=b.k.name.startswith(('f2fp_f32_to_8b_downconvert__',
                                 'uf2fp_f32_to_8b_downconvert__'))
    fp8_f16=b.k.name.startswith(('f2fp_f16_to_8b_downconvert__',
                                 'uf2fp_f16_to_8b_downconvert__'))
    if fp8_f32 or fp8_f16:
        if fp8_f32:
            a,c,carry=operands(b,'Ra/URa','Rb/URb/Sb','Rc/URc/Sc')
            sources=(a,c,carry)
        else:
            pair,carry=operands(b,'Rb/URb/Sb','Rc/URc/Sc')
            sources=(pair,carry)
        if check:
            return (all(sources) and dst is not None and dst.size==4 and
                all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive'
                    for n in sources) and
                all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode',
                                             'satfinite','relu','extract')))
        require(b,'dstfmt',['E4M3','E5M2'])
        require(b,'srcfmt',['F32'] if fp8_f32 else ['F16','BF16'])
        require(b,'merge',['PACK_AB_MERGE_C'] if fp8_f32 else ['UNPACK_B_MERGE_C'])
        require(b,'rndMode',['RN'])
        require(b,'satfinite',['SATFINITE']);require(b,'relu',['norelu','RELU'])
        require(b,'extract',['H0','H1'])
        enum=b.g.arch.enums[b.k.operand_types['dstfmt'].type]
        e4=enum['E4M3'];e5=enum['E5M2']
        mbits=b.select('dstfmt',{e4:'3:4',e5:'2:4'},4)
        bias=b.select('dstfmt',{e4:'7:4',e5:'15:4'},4)
        maxfinite=b.select('dstfmt',{e4:'0x7e:4',e5:'0x7b:4'},4)

        def convert(bits,prefix):
            sign=b.local(4,f'({bits} >> 24) & 0x80')
            exp=b.local(4,f'({bits} >> 23) & 0xff')
            frac=b.local(4,f'{bits} & 0x7fffff')
            sig=b.local(4,f'{frac.symbol} | 0x800000')
            target_exp=b.local(4,f'{exp.symbol} - 127:4 + {bias}')
            is_sub=b.local(1,f'{target_exp.symbol} s<= 0')
            drop=b.local(4,f'23:4 - {mbits}')
            extra=b.local(4,f'zext({is_sub.symbol}) * (1:4 - {target_exp.symbol})')
            shift=b.local(4,f'{drop.symbol} + {extra.symbol}')
            kept=b.local(4,'0');rem=b.local(4,'0');rounded=b.local(4,'0')
            em=b.local(4,'0');mag=b.local(4,'0');byte=b.local(4,'0')
            nan=b.local(1,f'({exp.symbol} == 0xff) && ({frac.symbol} != 0)')
            b.body += [f'if ({exp.symbol} == 0xff) goto <{prefix}_special>;',
                f'if ({shift.symbol} > 31:4) goto <{prefix}_tiny>;',
                f'{kept.symbol} = {sig.symbol} >> {shift.symbol};',
                f'{rem.symbol} = {sig.symbol} & ((1:4 << {shift.symbol}) - 1:4);',
                f'if ({rem.symbol} > (1:4 << ({shift.symbol} - 1:4))) goto <{prefix}_increment>;',
                f'if ({rem.symbol} < (1:4 << ({shift.symbol} - 1:4))) goto <{prefix}_pack>;',
                f'if (({kept.symbol} & 1) == 0) goto <{prefix}_pack>;',
                f'<{prefix}_increment>',f'{rounded.symbol} = {kept.symbol} + 1:4; goto <{prefix}_encoded>;',
                f'<{prefix}_pack>',f'{rounded.symbol} = {kept.symbol};',
                f'<{prefix}_encoded>',
                f'if ({is_sub.symbol}) goto <{prefix}_subnormal>;',
                f'{em.symbol} = {rounded.symbol} + (({target_exp.symbol} - 1:4) << {mbits});',
                f'goto <{prefix}_finite>;',
                f'<{prefix}_subnormal>',f'{em.symbol} = {rounded.symbol};',
                f'<{prefix}_finite>',
                f'if ({em.symbol} > {maxfinite}) goto <{prefix}_saturate>;',
                f'{mag.symbol} = {em.symbol}; goto <{prefix}_result>;',
                f'<{prefix}_saturate>',f'{mag.symbol} = {maxfinite};',
                f'goto <{prefix}_result>;',
                f'<{prefix}_special>',
                f'if ({nan.symbol}) goto <{prefix}_nan>;',
                f'{mag.symbol} = {maxfinite}; goto <{prefix}_result>;',
                f'<{prefix}_nan>',f'{mag.symbol} = 0x7f:4; goto <{prefix}_result>;',
                f'<{prefix}_tiny>',f'{mag.symbol} = 0:4;',
                f'<{prefix}_result>',
                f'{byte.symbol} = {sign.symbol} | {mag.symbol};',
                f'if ({b.values["relu"].symbol} != {b.g.arch.enums[b.k.operand_types["relu"].type]["RELU"]}:4) goto <{prefix}_relu_done>;',
                f'if ({nan.symbol} != 0:1) goto <{prefix}_relu_done>;',
                f'if (({byte.symbol} & 0x80:4) == 0:4) goto <{prefix}_relu_done>;',
                f'{byte.symbol} = 0:4;',f'<{prefix}_relu_done>']
            return byte

        if fp8_f32:
            av=convert(b.values[a].symbol,'f2fp_fp8_a')
            bv=convert(b.values[c].symbol,'f2fp_fp8_b')
        else:
            packed=b.values[pair].symbol
            lanes=[]
            formats=b.g.arch.enums[b.k.operand_types['srcfmt'].type]
            f16=formats.get('F16');bf16=formats.get('BF16')
            for lane in range(2):
                raw_word=b.expression(4,f'({packed} >> {lane*16}:4) & 0xffff:4')
                raw=b.local(2,f'{raw_word.symbol}:2')
                expanded=b.local(4,f'zext({raw.symbol}) << 16:4')
                # The F16 operand is reinterpreted as a 16-bit float and widened;
                # BF16 already has the same upper sixteen bits as F32.
                f32bits=b.local(4,f'float2float({raw.symbol})')
                src_cases={}
                if f16 is not None:src_cases[f16]=f32bits.symbol
                if bf16 is not None:src_cases[bf16]=expanded.symbol
                bits=b.select('srcfmt',src_cases,4)
                lanes.append(convert(bits,f'f2fp_fp8_half{lane}'))
            av,bv=lanes
        cv=b.values[carry].symbol;extract=b.values['extract'].symbol
        h1=b.g.arch.enums[b.k.operand_types['extract'].type]['H1']
        carryhalf=b.local(4,'0')
        b.body += [f'if ({extract} == {h1}:4) goto <f2fp_fp8_h1>;',
            f'{carryhalf.symbol} = {cv} & 0xffff:4; goto <f2fp_fp8_pack>;',
            '<f2fp_fp8_h1>',f'{carryhalf.symbol} = ({cv} >> 16) & 0xffff;',
            '<f2fp_fp8_pack>',
            f'{dst.symbol} = ({carryhalf.symbol} << 16) | '
            f'({bv.symbol if fp8_f16 else av.symbol} << 8) | '
            f'{av.symbol if fp8_f16 else bv.symbol};']
        return

    merge_c=b.k.name.startswith('f2fp_merge_c__')
    if not (merge_c or b.k.name.startswith('f2fp__')):return False
    if merge_c:
        a,c=operands(b,'Rb/URb/Sb','Rc/URc/Sc')
    else:
        a,c=operands(b,'Ra/URa','Rb/URb/Sb')
    if check:
        return (a is not None and c is not None and dst is not None and
                dst.size==4 and all(b.values[n].size==4 and
                b.values[n].kind!='runtime-primitive' for n in (a,c)) and
                all(n in b.values for n in ('dstfmt','srcfmt','merge','rndMode','relu')) and
                (not merge_c or 'extract' in b.values))
    require(b,'dstfmt',['F16','BF16','E6M9'])
    require(b,'srcfmt',['F32']);require(b,'merge',['MERGE_C' if merge_c else 'PACK_AB'])
    require(b,'rndMode',['RN','RZ']);require(b,'relu',['norelu','RELU'])
    if 'satfinite' in b.values:require(b,'satfinite',['nosatfinite','SATFINITE'])
    rnd=b.values['rndMode'].symbol;rz=b.g.arch.enums[b.k.operand_types['rndMode'].type]['RZ']
    fmt=b.values['dstfmt'].symbol;formats=b.g.arch.enums[b.k.operand_types['dstfmt'].type]
    bf16=formats.get('BF16')
    e6m9=formats.get('E6M9')
    relu=b.values['relu'].symbol
    relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
    satfinite=b.values.get('satfinite')
    satfinite_value=(b.g.arch.enums[b.k.operand_types['satfinite'].type]['SATFINITE']
                     if satfinite else None)

    def convert(src,prefix):
        bits=b.values[src].symbol;out=b.local(4,'0')
        if e6m9 is not None:
            exp6=b.local(4,f'({bits} >> 23) & 0xff:4')
            frac6=b.local(4,f'{bits} & 0x7fffff:4')
            sign6=b.local(4,f'({bits} >> 16) & 0x8000:4')
            e6=b.local(4,f'{exp6.symbol} - 127:4')
            sig6=b.local(4,f'{frac6.symbol} | 0x800000:4')
            q6=b.local(4,'0');rem6=b.local(4,'0');shift6=b.local(4,'0')
        if e6m9 is not None:
            b.body += [f'if ({fmt} == {e6m9}:4) goto <{prefix}_e6m9>;']
        if bf16 is not None:
            b.body += [f'if ({fmt} != {bf16}:4) goto <{prefix}_f16>;',
                f'local {prefix}_exp:4 = ({bits} >> 23) & 0xff;',
                f'local {prefix}_frac:4 = {bits} & 0x7fffff;',
                f'if ({prefix}_exp == 0xff && {prefix}_frac != 0) goto <{prefix}_bf_nan>;',
                f'if ({rnd} == {rz}:4) goto <{prefix}_bf_rz>;',
                f'local {prefix}_lsb:4 = ({bits} >> 16) & 1;',
                f'{out.symbol} = ({bits} + 0x7fff:4 + {prefix}_lsb) >> 16; goto <{prefix}_done>;',
                f'<{prefix}_bf_rz>',f'{out.symbol} = {bits} >> 16; goto <{prefix}_done>;',
                f'<{prefix}_bf_nan>',f'{out.symbol} = 0x7fff:4; goto <{prefix}_done>;',
                f'<{prefix}_f16>']
        # IEEE binary32 -> binary16. Integer rounding avoids the emulator's
        # minimum-subnormal fptrunc defect and models RN-even / truncation.
        sign=b.local(4,f'({bits} >> 16) & 0x8000')
        exp=b.local(4,f'({bits} >> 23) & 0xff')
        frac=b.local(4,f'{bits} & 0x7fffff')
        sig=b.local(4,f'{frac.symbol} | 0x800000')
        e=b.local(4,f'{exp.symbol} - 127:4')
        q=b.local(4,'0');rem=b.local(4,'0');shift=b.local(4,'0')
        b.body += [f'if ({exp.symbol} == 0xff) goto <{prefix}_h_special>;',
            f'if ({exp.symbol} == 0) goto <{prefix}_h_zero>;',
            f'if ({e.symbol} s> 15:4) goto <{prefix}_h_overflow>;',
            f'if ({e.symbol} s>= -14:4) goto <{prefix}_h_normal>;',
            f'if ({e.symbol} s< -25:4) goto <{prefix}_h_zero>;',
            f'{shift.symbol} = 0:4 - {e.symbol} - 1:4;',
            f'{q.symbol} = {sig.symbol} >> {shift.symbol};',
            f'{rem.symbol} = {sig.symbol} & ((1:4 << {shift.symbol}) - 1:4);',
            f'goto <{prefix}_h_round>;',
            f'<{prefix}_h_normal>',f'{shift.symbol} = 13:4;',
            f'{q.symbol} = {sig.symbol} >> 13;',
            f'{rem.symbol} = {sig.symbol} & 0x1fff;',
            f'<{prefix}_h_round>',f'if ({rnd} == {rz}:4) goto <{prefix}_h_pack>;',
            f'if ({rem.symbol} > (1:4 << ({shift.symbol} - 1:4))) goto <{prefix}_h_increment>;',
            f'if ({rem.symbol} < (1:4 << ({shift.symbol} - 1:4))) goto <{prefix}_h_pack>;',
            f'if (({q.symbol} & 1) == 0) goto <{prefix}_h_pack>;',
            f'<{prefix}_h_increment>',f'{q.symbol} = {q.symbol} + 1:4;',
            f'<{prefix}_h_pack>',f'if ({e.symbol} s>= -14:4) goto <{prefix}_h_norm_pack>;',
            f'{out.symbol} = {sign.symbol} | {q.symbol}; goto <{prefix}_done>;',
            f'<{prefix}_h_norm_pack>',
            f'if ({q.symbol} < 0x800:4) goto <{prefix}_h_norm_bits>;',
            f'{q.symbol} = 0x400:4; {e.symbol} = {e.symbol} + 1:4;',
            f'<{prefix}_h_norm_bits>',
            f'if ({e.symbol} s> 15:4) goto <{prefix}_h_overflow>;',
            f'{out.symbol} = {sign.symbol} | (({e.symbol} + 15:4) << 10) | ({q.symbol} & 0x3ff); goto <{prefix}_done>;',
            f'<{prefix}_h_special>',
            f'if ({frac.symbol} == 0) goto <{prefix}_h_inf>;',
            f'{out.symbol} = {sign.symbol} | 0x7e00:4; goto <{prefix}_done>;',
            f'<{prefix}_h_inf>',f'{out.symbol} = {sign.symbol} | 0x7c00:4; goto <{prefix}_done>;',
            f'<{prefix}_h_overflow>',
            f'if ({rnd} == {rz}:4) goto <{prefix}_h_maxfinite>;',
            f'{out.symbol} = {sign.symbol} | 0x7c00:4; goto <{prefix}_done>;',
            f'<{prefix}_h_maxfinite>',f'{out.symbol} = {sign.symbol} | 0x7bff:4; goto <{prefix}_done>;',
            f'<{prefix}_h_zero>',f'{out.symbol} = {sign.symbol};',f'<{prefix}_done>']
        if e6m9 is not None:
            b.body += [f'goto <{prefix}_end>;',f'<{prefix}_e6m9>',
                f'if ({exp6.symbol} == 0xff:4) goto <{prefix}_e6_special>;',
                f'if ({exp6.symbol} == 0:4) goto <{prefix}_e6_zero>;',
                f'if ({e6.symbol} s> 31:4) goto <{prefix}_e6_overflow>;',
                f'if ({e6.symbol} s< -40:4) goto <{prefix}_e6_zero>;',
                f'if ({e6.symbol} s>= -30:4) goto <{prefix}_e6_normal>;',
                f'{shift6.symbol} = 0:4 - {e6.symbol} - 16:4;',
                f'{q6.symbol} = {sig6.symbol} >> {shift6.symbol};',
                f'{rem6.symbol} = {sig6.symbol} & ((1:4 << {shift6.symbol}) - 1:4);',
                f'goto <{prefix}_e6_round>;',
                f'<{prefix}_e6_normal>',f'{shift6.symbol} = 14:4;',
                f'{q6.symbol} = {sig6.symbol} >> 14:4;',
                f'{rem6.symbol} = {sig6.symbol} & 0x3fff:4;',
                f'<{prefix}_e6_round>',
                f'if ({rnd} == {rz}:4) goto <{prefix}_e6_pack>;',
                f'if ({rem6.symbol} > (1:4 << ({shift6.symbol} - 1:4))) goto <{prefix}_e6_inc>;',
                f'if ({rem6.symbol} < (1:4 << ({shift6.symbol} - 1:4))) goto <{prefix}_e6_pack>;',
                f'if (({q6.symbol} & 1:4) == 0:4) goto <{prefix}_e6_pack>;',
                f'<{prefix}_e6_inc>',f'{q6.symbol} = {q6.symbol} + 1:4;',
                f'<{prefix}_e6_pack>',
                f'if ({e6.symbol} s< -30:4) goto <{prefix}_e6_subnormal>;',
                f'if ({q6.symbol} < 0x400:4) goto <{prefix}_e6_normal_pack>;',
                f'{q6.symbol} = 0x200:4; {e6.symbol} = {e6.symbol} + 1:4;',
                f'if ({e6.symbol} s> 31:4) goto <{prefix}_e6_overflow>;',
                f'<{prefix}_e6_normal_pack>',
                f'{out.symbol} = {sign6.symbol} | (({e6.symbol} + 31:4) << 9:4) | ({q6.symbol} & 0x1ff:4);',
                f'goto <{prefix}_end>;',
                f'<{prefix}_e6_subnormal>',
                f'if ({q6.symbol} >= 0x200:4) goto <{prefix}_e6_min_normal>;',
                f'{out.symbol} = {sign6.symbol} | {q6.symbol}; goto <{prefix}_end>;',
                f'<{prefix}_e6_min_normal>',
                f'{out.symbol} = {sign6.symbol} | 0x200:4; goto <{prefix}_end>;',
                f'<{prefix}_e6_overflow>',
                f'if ({rnd} == {rz}:4) goto <{prefix}_e6_maxfinite>;',
                f'{out.symbol} = {sign6.symbol} | 0x7e00:4; goto <{prefix}_end>;',
                f'<{prefix}_e6_maxfinite>',
                f'{out.symbol} = {sign6.symbol} | 0x7dff:4; goto <{prefix}_end>;',
                f'<{prefix}_e6_zero>',f'{out.symbol} = {sign6.symbol}; goto <{prefix}_end>;',
                f'<{prefix}_e6_special>',
                f'if ({frac6.symbol} != 0:4) goto <{prefix}_e6_nan>;',
                f'{out.symbol} = {sign6.symbol} | 0x7e00:4; goto <{prefix}_end>;',
                f'<{prefix}_e6_nan>',f'{out.symbol} = 0x7fff:4;',
                f'<{prefix}_end>']
        return out

    av=convert(a,'f2fp_a')
    def postprocess(value,prefix):
        # .SATFINITE maps infinities and overflow to signed MAX_NORM. .RELU
        # clamps negative non-NaN outputs to +0; NaNs remain canonical.
        done=f'{prefix}_mods_done'
        if e6m9 is not None:
            b.body += [f'if ({fmt} != {e6m9}:4) goto <{prefix}_not_e6m9>;',
                f'if ({satfinite.symbol if satfinite else "0:4"} != {satfinite_value if satfinite else "0"}:4) goto <{prefix}_e6_relu>;',
                f'if (({value.symbol} & 0x7fff:4) != 0x7e00:4) goto <{prefix}_e6_relu>;',
                f'{value.symbol} = ({value.symbol} & 0x8000:4) | 0x7dff:4;',
                f'<{prefix}_e6_relu>',
                f'if ({relu} != {relu_value}:4) goto <{done}>;',
                f'if (({value.symbol} & 0x8000:4) == 0:4) goto <{done}>;',
                f'if ((({value.symbol} & 0x7e00:4) == 0x7e00:4) && (({value.symbol} & 0x01ff:4) != 0:4)) goto <{done}>;',
                f'{value.symbol} = 0:4; goto <{done}>;',
                f'<{prefix}_not_e6m9>']
        b.body += [f'if ({fmt} == {bf16}:4) goto <{prefix}_bf16_mod>;',
            f'goto <{prefix}_f16_mod>;',
            f'<{prefix}_f16_mod>']
        if satfinite:
            b.body += [f'if ({satfinite.symbol} != {satfinite_value}:4) goto <{prefix}_f16_relu>;',
                f'if (({value.symbol} & 0x7fff:4) != 0x7c00:4) goto <{prefix}_f16_relu>;',
                f'{value.symbol} = ({value.symbol} & 0x8000:4) | 0x7bff:4;',
                f'<{prefix}_f16_relu>']
        b.body += [f'if ({relu} != {relu_value}:4) goto <{done}>;',
            f'if (({value.symbol} & 0x8000:4) == 0:4) goto <{done}>;',
            f'if (({value.symbol} & 0x7c00:4) == 0x7c00:4 && ({value.symbol} & 0x03ff:4) != 0:4) goto <{done}>;',
            f'{value.symbol} = 0:4; goto <{done}>;',
            f'<{prefix}_bf16_mod>']
        if satfinite:
            b.body += [f'if ({satfinite.symbol} != {satfinite_value}:4) goto <{prefix}_bf16_relu>;',
                f'if (({value.symbol} & 0x7fff:4) != 0x7f80:4) goto <{prefix}_bf16_relu>;',
                f'{value.symbol} = ({value.symbol} & 0x8000:4) | 0x7f7f:4;',
                f'<{prefix}_bf16_relu>']
        b.body += [f'if ({relu} != {relu_value}:4) goto <{done}>;',
            f'if (({value.symbol} & 0x8000:4) == 0:4) goto <{done}>;',
            f'if (({value.symbol} & 0x7f80:4) == 0x7f80:4 && ({value.symbol} & 0x007f:4) != 0:4) goto <{done}>;',
            f'{value.symbol} = 0:4;',f'<{done}>']
    postprocess(av,'f2fp_a')
    if merge_c:
        extract=b.values['extract'].symbol
        h1=b.g.arch.enums[b.k.operand_types['extract'].type]['H1']
        base=b.values[c].symbol
        b.body += ['local f2fp_merge_half:4 = 0:4;',
            f'if ({extract} == {h1}:4) goto <f2fp_merge_h1>;',
            f'f2fp_merge_half = {base} & 0xffff:4; goto <f2fp_merge_pack>;',
            '<f2fp_merge_h1>',
            f'f2fp_merge_half = ({base} >> 16:4) & 0xffff:4;',
            '<f2fp_merge_pack>',
            f'{dst.symbol} = (f2fp_merge_half << 16:4) | ({av.symbol} & 0xffff:4);']
    else:
        bv=convert(c,'f2fp_b');postprocess(bv,'f2fp_b')
        b.body += [f'{dst.symbol} = (({av.symbol} & 0xffff:4) << 16) | ({bv.symbol} & 0xffff:4);']


def emit_F2IP(b,check=False):
    """Pack two RN-even, saturating F32-to-8-bit conversions plus C[15:0]."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/Sc/URc')
    if check:
        return (all((a,bb,c)) and output(b) is not None and output(b).size==4 and
                all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
                all(b.values[n].kind!='runtime-primitive' for n in (a,bb,c)))
    formats=enum_cases(b,'dstfmt',['S8','U8'])
    require(b,'srcfmt',['F32']);require(b,'rnd',['ROUND'])
    require(b,'ntz',['NTZ']);require(b,'relu',['norelu'])
    av,bv,cv=(b.flag(n) for n in (a,bb,c))

    def convert(src,prefix,signed):
        exp=f'{prefix}_exp';frac=f'{prefix}_frac';sig=f'{prefix}_sig'
        sign=f'{prefix}_sign';mag=f'{prefix}_mag';e=f'{prefix}_e'
        shift=f'{prefix}_shift';q=f'{prefix}_q';rem=f'{prefix}_rem'
        mask=f'{prefix}_mask';half=f'{prefix}_half';val=f'{prefix}_val';byte=f'{prefix}_byte'
        lines=[f'local {sign}:1 = ({src} >> 31) != 0;',
               f'local {exp}:4 = ({src} >> 23) & 0xff;',
               f'local {frac}:4 = {src} & 0x7fffff;',
               f'local {sig}:4 = {frac} | 0x800000;',
               f'local {mag}:4 = 0;',
               f'if ({exp} == 0xff) goto <{prefix}_special>;',
               f'if ({exp} == 0) goto <{prefix}_done>;',
               f'local {e}:4 = {exp} - 127:4;',
               f'if ({e} s< -1:4) goto <{prefix}_done>;',
               f'if ({e} == -1:4) goto <{prefix}_halfway>;',
               f'if ({e} s>= {7 if signed else 8}:4) goto <{prefix}_overflow>;',
               f'local {shift}:4 = 23:4 - {e};',
               f'local {q}:4 = {sig} >> {shift};',
               f'local {mask}:4 = (1:4 << {shift}) - 1:4;',
               f'local {rem}:4 = {sig} & {mask};',
               f'local {half}:4 = 1:4 << ({shift} - 1:4);',
               f'if ({rem} > {half}) goto <{prefix}_increment>;',
               f'if ({rem} < {half}) goto <{prefix}_rounded>;',
               f'if (({q} & 1) == 0) goto <{prefix}_rounded>;',
               f'<{prefix}_increment>',f'{q} = {q} + 1:4;',
               f'<{prefix}_rounded>',f'{mag} = {q};',f'goto <{prefix}_done>;',
               f'<{prefix}_halfway>',
               f'if ({sig} <= 0x800000) goto <{prefix}_done>;',
               f'{mag} = 1;',f'goto <{prefix}_done>;',
               f'<{prefix}_special>',
               f'if ({frac} != 0) goto <{prefix}_done>;',
               f'<{prefix}_overflow>',
               f'{mag} = {("128" if signed else "255")};',
               f'<{prefix}_done>']
        if signed:
            lines += [f'local {val}:4 = {mag};',
                      f'if ({sign} == 0) goto <{prefix}_positive>;',
                      f'{val} = 0:4 - {mag}; goto <{prefix}_packed>;',
                      f'<{prefix}_positive>',
                      f'if ({mag} <= 127) goto <{prefix}_packed>;',
                      f'{val} = 127;',f'<{prefix}_packed>',
                      f'local {byte}:4 = {val} & 0xff;']
        else:
            lines += [f'local {byte}:4 = 0;',
                      f'if ({sign} != 0) goto <{prefix}_unsigned_done>;',
                      f'{byte} = {mag};',
                      f'if ({byte} <= 255) goto <{prefix}_unsigned_done>;',
                      f'{byte} = 255;',f'<{prefix}_unsigned_done>']
        return ' '.join(lines),byte

    cases={}
    for label,value in formats.items():
        signed=label=='S8'
        ca,ba=convert(av,'fa',signed);cb,bbbyte=convert(bv,'fb',signed)
        code=(f'{ca} {cb} local acc:4 = {cv}; '
              f'local result:4 = ((acc & 0xffff) << 16) | ({ba} << 8) | {bbbyte}; '
              'export result;')
        cases[(value,)]=code
    result=b.select_code(('dstfmt',),cases)
    b.body.append(f'{output(b).symbol} = {result};')


def emit_I2IP(b,check=False):
    """Saturating integer conversion and packing used by PTX cvt.pack."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc')
    dst=output(b)
    if check:
        ok=(all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive'
                for n in (a,bb,c)))
        if not ok:b.unsupported_reason='I2IP requires three materialized 32-bit values'
        return ok

    require(b,'srcfmt',['S32'])
    formats=enum_cases(b,'dstfmt',['S2','U2','S4','U4','S8','U8','S16','U16'])
    extract_name=next((n for n,a in b.k.operand_types.items()
                       if a.type in ('EXTRACT','HSEL','ONLY24','ONLY28')),None)
    sat_name=next((n for n,a in b.k.operand_types.items()
                   if a.type in ('SATONLY','SATRELU','SATRELU_RELU')),None)
    extract_values=enum_cases(b,extract_name,['H0','H1','24','28']) if extract_name else {}
    sat_atom=b.k.operand_types.get(sat_name) if sat_name else None
    sat_enum=b.g.arch.enums.get(sat_atom.type,{}) if sat_atom else {}
    sat_labels=[name for name in ('nosatrelu','nosatrelu_relu','SAT','SATRELU','RELU') if name in sat_enum]
    sat_modes=enum_cases(b,sat_name,sat_labels) if sat_name else {}
    av,bv,cv=(b.flag(n) for n in (a,bb,c))
    cases={}
    selector_names=('dstfmt',extract_name,sat_name)
    for fmt,fv in formats.items():
        signed=fmt.startswith('S');width=int(fmt[1:]);mask=(1<<width)-1
        for extract,xv in extract_values.items():
            # The ordinary byte/halfword classes expose H0/H1 extraction;
            # sub-byte pack classes encode the required cursor lane as .24/.28.
            if width<=4 and extract!=str(32-2*width):continue
            if width>=8 and extract not in ('H0','H1'):continue
            for mode,mv in sat_modes.items():
                do_sat=mode in ('SAT','SATRELU')
                do_relu=mode in ('RELU','SATRELU')
                if not signed and do_relu:continue
                lo=-(1<<(width-1)) if signed else 0
                hi=(1<<(width-1))-1 if signed else mask

                def convert(src,tag):
                    value=f'{tag}_value';packed=f'{tag}_packed'
                    code=f'local {value}:4 = {src}; '
                    if do_sat:
                        code+=(f'if ({value} s< {lo}) goto <{tag}_lo>; '
                               f'if ({value} s> {hi}) goto <{tag}_hi>; '
                               f'goto <{tag}_done>; <{tag}_lo> {value} = {lo}; '
                               f'goto <{tag}_done>; <{tag}_hi> {value} = {hi}; <{tag}_done> ')
                    if do_relu:
                        code+=(f'if ({value} s< 0) goto <{tag}_relu>; '
                               f'goto <{tag}_relu_done>; <{tag}_relu> {value} = 0; '
                               f'<{tag}_relu_done> ')
                    code+=f'local {packed}:4 = {value} & {mask}; '
                    return code,packed

                ca,pa=convert(av,'i2ip_a');cb,pb=convert(bv,'i2ip_b')
                carry_source=cv if extract=='H0' else f'(({cv}) >> 16)'
                tail=(f' | (({carry_source}) & 0x{(1<<(32-2*width))-1:x}) << {2*width}'
                      if width<16 else f' | (({cv}) & 0)')
                code=(ca+cb+f'local i2ip_result:4 = (zext({pa}) << {width}) | '
                      f'zext({pb}){tail}; export i2ip_result;')
                cases[(fv,xv,mv)]=code
    b.allow_combinations(selector_names,cases.keys())
    table=b.select_code(selector_names,cases)
    b.body.append(f'{dst.symbol} = {table};')


def emit_QSPC(b,check=False):
    """Query a caller-provided generic-address map via the runtime primitive."""
    if check:
        return ('Ra' in b.values and 'Ra_offset' in b.values and
                'space' in b.values and 'Pu' in b.outputs and
                output(b) is not None and b.values['Ra'].size in (4,8) and
                ('Ra_URb' not in b.values or b.values['Ra_URb'].size in (4,8)))
    # Rd reports a one-hot tag for the input pointer's space. SM89 GPU probes
    # establish G=1, S=2, L=4; D's nonzero-Rd tag is not hardware-verified.
    atom=b.k.operand_types.get('space')
    enum=b.g.arch.enums.get(atom.type,{}) if atom else {}
    labels=[x for x in ('G','L','S','D') if x in enum]
    b.selector_guards['space']=labels
    values={x:enum[x] for x in labels}
    ordinary={values[x] for x in ('G','L','S') if x in values}
    ordinary_clauses=b.g.values_where(b.k,'space',lambda v,w=ordinary:v in w)
    if ordinary_clauses is not None:b.selector_patterns.append(ordinary_clauses)
    combinations=[(x,'any') for x in ('G','L','S') if x in values]
    if 'D' in values and 'Rd' in b.outputs:
        zero=b.g.register_zero(b.k,'Rd')
        d_space=b.g.values_where(b.k,'space',lambda v,w=values['D']:v==w)
        d_zero=b.g.values_where(b.k,'Rd',lambda v,z=zero:v==z)
        if d_space is not None and d_zero is not None:
            b.selector_patterns.append(d_space+d_zero)
            combinations.append(('D','RZ'))
    b.selector_combinations['space,Rd']=combinations
    # The userop calculates both Pu (query match) and Rd (input space tag);
    # unsupported encodings use the complete opaque fallback.
    b.body+=b.call('prim')


def emit_UP2UR(b,check=False):
    """Pack an immediate mask of UP0..UP6 into a uniform integer register."""
    from sass.gen_sleigh import pieces
    source=b.g.source(b.k,'Sb')
    source_pieces=pieces(source[1]) if source and source[0]=='field' else []
    if check:
        return (b.k.name=='up2ur__Imm' and 'URa' in b.values and
                'Sb' in b.values and 'insert' in b.values and
                'URd' in b.outputs and b.values['URa'].size==4 and
                b.values['Sb'].size==4 and b.outputs['URd'].size==4 and
                len(source_pieces)==1 and source_pieces[0][3]>=7)
    insert=b.g.arch.enums[b.k.operand_types['insert'].type]
    b.allow('insert',lambda v,z=insert.get('B0'):v==z)
    base=b.values['URa'].symbol
    dest=b.outputs['URd'].symbol
    from sass import ir
    from sass.gen_sleigh import eq,pattern
    (tok,lo,hi,width),=source_pieces
    mask_field=ir.Field('',((lo+6,lo),))
    refs=[r for r in b.refs if r[1] in (base,dest)]
    constructors=[]
    for mask in range(128):
        body=[f'local packed:4 = {base};']
        body.extend(f'packed = packed | (zext(UP{bit}) << {bit});'
                    for bit in range(7) if mask & (1<<bit))
        body.append(f'{dest} = packed;')
        constructors.append(('',pattern(eq(b.g,mask_field,mask),refs),'',' '.join(body)))
    table=b.g.subtable('up2ur_mask',constructors)
    b.refs.append(('sub',table))
    b.body.append(f'build {table};')


def _predicate_transfer(b,op):
    if op=='P2R':
        source=next((n for n in ('Rb','URb','Sb') if n in b.values),None)
        selector='insert';dest=output(b);base=b.values.get('Ra')
        return source,selector,dest,base
    source=next((n for n in ('Rb','URb','Sb') if n in b.values),None)
    return source,'a_bsel',None,b.values.get('Ra')


def _emit_predicate_transfer(b,op,check=False):
    import re
    source,selector,dest,base=_predicate_transfer(b,op)
    if check:
        supported_source=(source is not None and
                          b.k.operand_types[source].type in ('Register','UniformRegister','SImm','UImm') and
                          not any(atom.type in ('C','CX') for atom in b.k.operand_types.values()))
        return (supported_source and base is not None and base.size==4 and
                (op=='R2P' or (dest is not None and dest.size==4)))
    selector_type=b.k.operand_types[selector].type
    selector_values=b.g.arch.enums[selector_type]
    byte_selectors={name:selector_values[name] for name in ('B0','B1','B2','B3')
                    if name in selector_values}
    b.allow(selector,lambda v,valid=frozenset(byte_selectors.values()):v in valid)
    # Uniform-register operands can be declared as a 64-bit pair in the md;
    # these transfers use only the low 32 mask bits.
    mask_value=b.values[source]
    mask_expr=(f'{mask_value.symbol}:4' if mask_value.size>4 else
               f'zext({mask_value.symbol})' if mask_value.size<4 else mask_value.symbol)
    mask='mask'
    from sass.gen_sleigh import pattern
    constructors=[]
    for byte,selector_value in byte_selectors.items():
        offset=8*int(byte[1])
        code=[f'local {mask}:4 = {mask_expr};']
        if op=='P2R':
            packed='packed'
            code.append(f'local {packed}:4 = 0;')
            for bit in range(7):
                skip=f'p2r_b{byte[1]}_skip_{bit}'
                code += [f'if ({mask}[{bit},1] == 0:1) goto <{skip}>;',
                         f'{packed} = {packed} | (zext(P{bit}) << {bit}:4);',
                         f'<{skip}>']
            clear=(~(0xff << offset)) & 0xffffffff
            code += [f'local result:4 = ({base.symbol} & 0x{clear:08x}:4) | '
                     f'({packed} << {offset}:4);',
                     f'{dest.symbol} = result;']
        else:
            for bit in range(7):
                skip=f'r2p_b{byte[1]}_skip_{bit}'
                code += [f'if ({mask}[{bit},1] == 0:1) goto <{skip}>;',
                         f'P{bit} = {base.symbol}[{offset+bit},1];',f'<{skip}>']
        names=set(re.findall(r'\b\w+\b',' '.join(code)))
        refs=[r for r in b.refs if r[1] in names]
        clauses=b.g.values_where(b.k,selector,lambda v,w=selector_value:v==w)
        constructors.append(('""',pattern(clauses,refs),'',' '.join(code)))
    table=b.g.subtable(f'{op.lower()}_byte',constructors)
    b.refs.append(('sub',table))
    b.body.append(f'build {table};')


def emit_P2R(b,check=False):
    return _emit_predicate_transfer(b,'P2R',check)


def emit_R2P(b,check=False):
    return _emit_predicate_transfer(b,'R2P',check)


def emit_DSETP(b,check=False):
    return float_compare(b,8,check)


def float_compare(b,size,check):
    selector='fcomp' if size==4 else 'test'
    names=operands(b,'Ra/Sa','Rb/Sb/URb' if size==4 else 'Rc/Sc/URc')
    if check:return all(names) and selector in b.values and output(b,'Pu') is not None and all(b.values[n].size==size for n in names)
    if size==4:_,_,(x,y)=float_inputs(b,names)
    else:x,y=[b.flag(n,True) for n in names]
    unordered=f'(nan({x}) || nan({y}))'
    ordered={'LT':f'{x} f< {y}','EQ':f'{x} f== {y}','LE':f'{x} f<= {y}','GT':f'{y} f< {x}',
             'NE':f'!{unordered} && ({x} f!= {y})','GE':f'{y} f<= {x}'}
    tests={**ordered,**{l+'U':f'{unordered} || ({e})' for l,e in ordered.items() if l!='NE'},
           'NEU':f'{x} f!= {y}','NUM':f'!{unordered}','NAN':unordered,'T':'1:1','F':'0:1'}
    en=enum_cases(b,selector,list(tests))
    cond=b.select_code((selector,),{(v,):f'local t:1 = {tests[l]}; export t;' for l,v in en.items()})
    if 'bop' not in b.values:
        b.body.append(f'{output(b,"Pu").symbol} = {cond};');return
    pv=b.flag(operands(b,'Pp')[0]);yes=combine(b,cond,pv);no=combine(b,b.expression(1,f'!{cond}').symbol,pv)
    yes=b.local(1,yes).symbol;no=b.local(1,no).symbol
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {yes if name in ("Pu","UPu") else no};')


def emit_ISETP(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    extended='ex' in b.values
    if check:
        return (all((a,c,p)) and 'icmp' in b.values and 'bop' in b.values and
                b.values[a].size==b.values[c].size==4 and
                (('Pr' in b.values)==extended) and 'UPr' not in b.values)
    if ('Pr' in b.values)!=extended or 'UPr' in b.values:b.reject()
    require(b,'fmt',['U32','S32'])
    av,cv=b.flag(a),b.flag(c)
    cond=comparison(b,av,cv); pv=b.flag(p)
    if extended:
        ex_en=b.g.arch.enums[b.k.operand_types['ex'].type]
        ex_value=ex_en.get('EX')
        noex_value=ex_en.get('noex')
        if noex_value is not None:
            # Older architectures encode both modes in one class. Keep the
            # ordinary form's p-code free of the high-word and Pr reads.
            extended_cond=b.select_code(('ex',),{
                (noex_value,):f'local t:1 = {cond}; export t;',
                (ex_value,):f'local t:1 = {cond}; if ({av} != {cv}) goto <isept_ex_done>; '
                            f't = {b.flag("Pr")}; <isept_ex_done> export t;'
            })
            yes=combine(b,extended_cond,pv)
            no=combine(b,b.expression(1,f'!{extended_cond}').symbol,pv)
            yes=b.local(1,yes).symbol;no=b.local(1,no).symbol
            for name,out in b.outputs.items():
                b.body.append(f'{out.symbol} = {yes if name in ("Pu","UPu") else no};')
            return
        # ISETP.EX compares the high words first. If equal, Pr carries the
        # comparison result for the low words from the preceding ISETP.
        require(b,'bop',['AND','OR','XOR'])
        result=b.local(1,cond).symbol
        b.body += [f'if ({av} != {cv}) goto <isept_ex_done>;',
                   f'{result} = {b.flag("Pr")};','<isept_ex_done>']
        yes=b.local(1,'0').symbol;no=b.local(1,'0').symbol
        bop=b.values['bop'].symbol;en=b.g.arch.enums[b.k.operand_types['bop'].type]
        for label,operator in (('AND','&'),('OR','|'),('XOR','^')):
            value=en.get(label)
            if value is None:continue
            next_label=f'isept_ex_{label.lower()}';done='isept_ex_combine_done'
            b.body += [f'if ({bop} != {value}) goto <{next_label}>;',
                       f'{yes} = {result} {operator} {pv};',
                       f'{no} = !{result} {operator} {pv};',
                       f'goto <{done}>;',f'<{next_label}>']
        b.body.append('<isept_ex_combine_done>')
        for name,out in b.outputs.items():
            b.body.append(f'{out.symbol} = {yes if name in ("Pu","UPu") else no};')
        return
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
    if check:return all((a,c,scale)) and output(b) is not None and 'hilo' in b.values and all(b.values[n].size==4 for n in (a,c))
    extended='X' in b.values;p=operands(b,'Pp')[0]
    if bool(p)!=extended or any(n in b.values for n in ('Pr','UPr')):b.reject()
    require(b,'hilo',['LO','HI']);require(b,'sx32',['nosx32','NOSX32','SX32'])
    exclusive_modifiers(b,a,c,'invert' if extended else 'negate')
    mode=next(iter(b.g.arch.enums[b.k.operand_types['hilo'].type]))
    shift=b.values[scale].symbol
    if mode=='HI':
        if 'sx32' in b.values:source=b.expression(8,f'sext({b.values[a].symbol})').symbol
        else:
            high=operands(b,'Rc/Sc')[0]
            if not high or b.values[high].size!=4:b.reject();return
            source=b.expression(8,f'(zext({b.values[high].symbol}) << 32) | zext({b.values[a].symbol})').symbol
        shifted=b.expression(8,f'({source} << {shift}) >> 32').symbol
        word=b.expression(4,f'{shifted}:4').symbol
    else:word=b.expression(4,f'{b.values[a].symbol} << {shift}').symbol
    av,bv=modified_word(b,a,word),modified_word(b,c)
    slow=[f'local total:8 = {av} + {bv}'+(f' + zext({b.flag(p)})' if extended else '')+';',
          'local cout:1 = total >= 0x100000000:8;',f'{output(b).symbol} = total:4;']
    for name in ('Pu','UPu'):
        if name in b.outputs:slow.append(f'{b.outputs[name].symbol} = cout;')
    # With discarded carry, low-word arithmetic retains ordinary compact code.
    af=b.select(a+'@negate',{0:word,1:f'-{word}'},4) if a+'@negate' in b.values else b.select(a+'@invert',{0:word,1:f'~{word}'},4) if a+'@invert' in b.values else word
    fast=[f'local total:4 = {af} + {b.flag(c)}'+(f' + zext({b.flag(p)})' if extended else '')+';',f'{output(b).symbol} = total;']
    carry_choice(b,fast,slow,['Pu','UPu'])


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
