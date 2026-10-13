"""Class-specific native overrides and explicit instruction primitives.

Unsupported selector combinations decode to complete-input opaque operations.
This module does not equate PTX syntax with undocumented SASS variants.
"""
from dataclasses import dataclass
from itertools import product
from sass import fpops
from sass.sem_family import FAMILIES, family_for
import sass.families  # registers the semantics families

OPERATION_FAMILIES = set('IADD3 IMAD LOP3 SHF BMSK LEA IABS IMNMX ISETP SEL PRMT MOV S2R CS2R LDC FADD FMUL FFMA FSETP F2I F2FP I2F I2FP MUFU LDG STG LDS STS LDL STL BRA EXIT CALL RET BSSY BSYNC BAR SHFL VOTE ULDC NOP'.split())
ALIASES = dict(UIADD3='IADD3',
               UF2FP='F2FP', **{'HFMA2.MMA':'HFMA2_MMA'})
OPERATION_FAMILIES.update(ALIASES)
OPERATION_FAMILIES.update(('IADD','FSEL','FMNMX','FMNMX3','DADD','DMUL','DFMA','DSETP','F2IP','I2IP','HADD2','HFMA2','HMUL2','HMNMX2','BREV','POPC','FLO','SGXT','IDP','QSPC','UP2UR','P2R','R2P','VIMNMX','VIMNMX3','VIADDMNMX','VABSDIFF4'))

PRIMITIVES = set('FADD FMUL FFMA FSETP F2I I2F I2FP MUFU PRMT SHFL VOTE BAR BSSY BSYNC QSPC'.split())

@dataclass(frozen=True)
class Override:
    family: str
    implementation: str

OPERATION_FAMILIES.update(FAMILIES)

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


def dispatch(b,fallback='opaque',refs=None):
    """Native body (with its pattern references `refs`, or found by scanning the text for the
    hand-written emitters) behind the selector guards, and the opaque or primitive fallback."""
    import re
    from sass.gen_sleigh import pattern
    from sass.semantic import prune_unused_values
    native=' '.join(b.body if refs is not None else prune_unused_values(b.body))
    if refs is None:refs=[r for r in b.refs if r[1] in set(re.findall(r'\b\w+\b',native))]
    fallback_lines=b.call(fallback)
    fallback=' '.join(fallback_lines)
    fallback_refs=(list(dict.fromkeys(b.call_refs)) if refs is not None
                   else [r for r in b.refs if r[1] in set(re.findall(r'\b\w+\b',fallback))])
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
    if family_for(b.k):
        from sass import sem_sleigh
        return sem_sleigh.emit(b)
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


def arithmetic(b,op,xs,size,rnd,scale=0):
    return fpops.arithmetic(op,xs,size,rnd,scale,exact=not fpops.readable(b))


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
