"""Class-specific native overrides and explicit instruction primitives.

Unsupported selector combinations decode to complete-input opaque operations.
This module does not equate PTX syntax with undocumented SASS variants.
"""
from dataclasses import dataclass
from itertools import product
from sass import fpops

OPERATION_FAMILIES = set('IADD3 IMAD LOP3 SHF BMSK LEA IABS IMNMX ISETP SEL PRMT MOV S2R CS2R LDC FADD FMUL FFMA FSETP F2I I2F I2FP MUFU LDG STG LDS STS LDL STL BRA EXIT CALL RET BSSY BSYNC BAR SHFL VOTE ULDC NOP'.split())
ALIASES = dict(UIADD3='IADD3', UIMAD='IMAD', USHF='SHF', ULEA='LEA',
               UISETP='ISETP', ULOP3='LOP3', USEL='SEL', UMOV='MOV', VIADD='IADD',
               **{'HFMA2.MMA':'HFMA2_MMA'})
OPERATION_FAMILIES.update(ALIASES)
OPERATION_FAMILIES.update(('IADD','FSEL','FMNMX','FMNMX3','DADD','DMUL','DFMA','DSETP','F2IP','I2IP','HADD2','HFMA2','HMUL2','BREV','POPC','FLO','SGXT','IDP','VIMNMX','VIMNMX3','VIADDMNMX','VABSDIFF4'))

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
        mode=b.k.operand_types.get('wide')
        labels=b.g.arch.enums.get(mode.type,{}) if mode else {}
        if set(labels)&{'HI','WIDE'} and b.k.operand_types[d].type in ('C','CX') and b.values[d].size<8:
            b.unsupported_reason='32-bit constant addend extension for HI/WIDE is unverified'
            return False
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
        if b.values[d].size<8:rc=b.local(8,f'zext({rc})').symbol
        summed=b.local(8,f'{product.symbol} + {rc}')
        result=b.local(8,f'{summed.symbol} + zext({b.flag(carryin)})') if extended else summed
        full=result
        if mode=='HI':result=b.local(8,f'{result.symbol} >> 32')
    fast=b.body[initial:]+[f'{rd.symbol} = {result.symbol}{":4" if result.size>rd.size else ""};']
    b.body=b.body[:initial]
    if mode=='LO':b.body+=fast;return
    predicate=output(b,'Pu')
    if predicate:
        overflow=f'({summed.symbol} < {product.symbol})'
        if extended:overflow+=f' || ({full.symbol} < {summed.symbol})'
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
    """Four packed byte products plus a 32-bit accumulator (IDP.4A)."""
    if not b.k.name.startswith('idp_4a__'):
        if check:b.unsupported_reason='only IDP.4A forms are implemented'
        return False if check else None
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc');dst=output(b)
    if check:
        ok=(all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
            b.k.operand_types[bb].type!='CX')
        if not ok:b.unsupported_reason='IDP.4A requires 32-bit non-CX sources'
        return ok
    require(b,'mode',['4A']);require(b,'SrcAFmt',['S8','U8']);require(b,'SrcBFmt',['S8','U8'])
    av=b.flag(a);bv=b.flag(bb);cv=b.flag(c)
    cases={}
    for asigned,aval in enum_cases(b,'SrcAFmt',['S8','U8']).items():
        for bsigned,bval in enum_cases(b,'SrcBFmt',['S8','U8']).items():
            code=f'local aa:4 = {av}; local bbv:4 = {bv}; local acc:4 = {cv}; '
            for i in range(4):
                code+=f'local ashr{i}:4 = aa >> {8*i}; local bshr{i}:4 = bbv >> {8*i}; local abyte{i}:1 = ashr{i}[0,8]; local bbyte{i}:1 = bshr{i}[0,8]; '
                ax=f'sext(abyte{i})' if asigned=='S8' else f'zext(abyte{i})'
                bx=f'sext(bbyte{i})' if bsigned=='S8' else f'zext(bbyte{i})'
                code+=f'acc = acc + ({ax} * {bx}); '
            code+='export acc;'
            cases[(aval,bval)]=code
    result=b.select_code(('SrcAFmt','SrcBFmt'),cases)
    b.body.append(f'{dst.symbol} = {result};')


def emit_VIMNMX3(b,check=False):
    """Per-lane three-way integer min/max, with the CuLifter RELU option."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/URc/Sc');dst=output(b)
    if check:
        ok=(b.k.name.startswith('vimnmx3__') and all((a,bb,c)) and dst is not None and dst.size==4 and
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
            all(b.k.operand_types[n].type!='CX' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VIMNMX3 requires three 32-bit non-CX values'
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
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb)) and
            all(b.k.operand_types[n].type!='CX' for n in (a,bb)))
        if not ok:b.unsupported_reason='VIMNMX requires two 32-bit non-CX values'
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
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
            all(b.k.operand_types[n].type!='CX' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VIADDMNMX requires three 32-bit non-CX values'
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
            all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
            all(b.k.operand_types[n].type!='CX' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='VABSDIFF4 requires three 32-bit non-CX inputs'
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
        elif b.values[name].size!=4:return None
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
    if check:return bool(names)
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
        return (all(names) and not any(b.k.operand_types[n].type=='CX' for n in names) and
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
    # .XORSIGN changes the formula; exact .NAN requires hardware evidence.
    require(b,'xorsign',['noxorsign'])
    _,_,(a,c)=float_inputs(b,names)
    predicate=b.flag(p)
    if fpops.readable(b):
        enum_cases(b,'nan',['nonan','NAN'])
        result=b.local(4,c)
        b.body += [f'if (({a} f< {c}) != {predicate}) goto <minmaxdone>;',f'{result.symbol} = {a};',
                   '<minmaxdone>',f'{output(b).symbol} = {result.symbol};']
        return
    require(b,'nan',['nonan'])
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


def emit_FMNMX3(b,check=False):
    """Three-input scalar float min/max selected by the predicate value."""
    names=float_shape(b,'Ra/Sa','Rb/Sb/URb','Rc/Sc/URc')
    p=operands(b,'Pp')[0]
    if check:return bool(names and p) and set(b.outputs)=={'Rd'}
    # CuLifter's implementation uses ordered comparisons and a two-step
    # select. Keep FTZ/NAN selectors opaque until their behavior is verified.
    require(b,'ftz',['noftz'])
    require(b,'nan',['nonan'])
    require(b,'Pp',['PT'])
    _,_,(a,c,d)=float_inputs(b,names)
    pred=b.flag(p)
    first=b.local(4,a)
    second=b.local(4,'0')
    third=b.local(4,'0')
    choose_c=f'(({pred} != 0 && {first.symbol} f> {c}) || ({pred} == 0 && {first.symbol} f< {c}))'
    choose_d=f'(({pred} != 0 && {second.symbol} f> {d}) || ({pred} == 0 && {second.symbol} f< {d}))'
    b.body += [f'if {choose_c} goto <take_c>; {second.symbol} = {first.symbol}; goto <first_done>;',
               '<take_c>',f'{second.symbol} = {c};','<first_done>',
               f'if {choose_d} goto <take_d>; {third.symbol} = {second.symbol}; goto <second_done>;',
               '<take_d>',f'{third.symbol} = {d};','<second_done>',
               f'{output(b).symbol} = {third.symbol};']


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
    # Keep absolute/negate source modifiers on the primitive path until their
    # SASS interaction with NTZ is independently verified.
    bits=b.values[src].symbol
    for attr in ('absolute','negate'):
        name=src+'@'+attr
        if name in b.values:b.allow(name,lambda v:v==0)
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


def emit_F2IP(b,check=False):
    """Pack two RN-even, saturating F32-to-8-bit conversions plus C[15:0]."""
    a,bb,c=operands(b,'Ra','Rb/URb/Sb','Rc/Sc/URc')
    if check:
        return (all((a,bb,c)) and output(b) is not None and output(b).size==4 and
                all(b.values[n].size==4 and b.values[n].kind!='runtime-primitive' for n in (a,bb,c)) and
                all(b.k.operand_types[n].type!='CX' for n in (a,bb,c)))
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
                for n in (a,bb,c)) and
            all(b.k.operand_types[n].type!='CX' for n in (a,bb,c)))
        if not ok:b.unsupported_reason='I2IP requires three 32-bit non-CX values'
        return ok

    require(b,'srcfmt',['S32'])
    formats=enum_cases(b,'dstfmt',['S2','U2','S4','U4','S8','U8','S16','U16'])
    extract_name=next((n for n,a in b.k.operand_types.items()
                       if a.type in ('EXTRACT','HSEL','ONLY24','ONLY28')),None)
    sat_name=next((n for n,a in b.k.operand_types.items()
                   if a.type in ('SATONLY','SATRELU','SATRELU_RELU')),None)
    extract_values=enum_cases(b,extract_name,['H0','24','28']) if extract_name else {}
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
            if width>=8 and extract!='H0':continue
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
                tail=(f' | (({cv}) & 0x{(1<<(32-2*width))-1:x}) << {2*width}'
                      if width<16 else f' | (({cv}) & 0)')
                code=(ca+cb+f'local i2ip_result:4 = (zext({pa}) << {width}) | '
                      f'zext({pb}){tail}; export i2ip_result;')
                cases[(fv,xv,mv)]=code
    b.allow_combinations(selector_names,cases.keys())
    table=b.select_code(selector_names,cases)
    b.body.append(f'{dst.symbol} = {table};')


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
