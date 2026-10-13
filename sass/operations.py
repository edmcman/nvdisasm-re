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


def enum_cases(b,name,labels):
    """{label: value} for the labels of selector `name` this class can encode; {None: None} if absent."""
    if name not in b.values:return {None:None}
    en=b.g.arch.enums[b.k.operand_types[name].type]
    require(b,name,[l for l in labels if l in en])
    return {l:en[l] for l in labels if l in en}


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

    s2f6_f32=b.k.name.startswith('f2fp_f32_to_mx8_downconvert_scale__')
    s2f6_bf16=b.k.name.startswith('f2fp_f16_to_mx8_downconvert_scale__')
    if s2f6_f32 or s2f6_bf16:
        if s2f6_f32:
            a,bb,scale=operands(b,'Ra/URa','Rb/URb/Sb','Rc/URc/Sc')
            sources=(a,bb,scale)
        else:
            pair,scale=operands(b,'Rb/URb/Sb','Rc/URc/Sc')
            sources=(pair,scale)
        if check:
            return (all(sources) and dst is not None and dst.size==4 and
                all(b.values[n].size in (4,8) and b.values[n].kind!='runtime-primitive'
                    for n in sources) and
                all(n in b.values for n in
                    ('dstfmt','srcfmt','merge','rndMode','relu','satnarrow')))
        require(b,'dstfmt',['S2_6'])
        require(b,'srcfmt',['F32'] if s2f6_f32 else ['BF16'])
        require(b,'merge',['PACK_AB_MERGE_C'] if s2f6_f32 else ['UNPACK_B_MERGE_C'])
        require(b,'rndMode',['RN'])
        require(b,'relu',['norelu','RELU'])
        # PTX ISA's S2F6 conversion requires satfinite. SATNARROW is a
        # distinct SASS selector and remains on the opaque path until its
        # behavior is established independently.
        require(b,'satnarrow',['SATFINITE'])
        relu_value=b.g.arch.enums[b.k.operand_types['relu'].type]['RELU']
        relu=b.values['relu'].symbol
        scale_word=b.values[scale].symbol

        def convert_s2f6(bits,scale_byte,prefix):
            scale=b.local(4,scale_byte)
            sign=b.local(4,f'{bits} & 0x80000000:4')
            exp=b.local(4,f'({bits} >> 23:4) & 0xff:4')
            frac=b.local(4,f'{bits} & 0x7fffff:4')
            out=b.local(4,'0')
            scaled=b.local(4,'0');scaled64=b.local(4,'0')
            scaled_abs=b.local(4,'0');scaled_exp=b.local(4,'0')
            significand=b.local(4,'0');integer_exp=b.local(4,'0')
            trunc_f=b.local(4,'0');residual=b.local(4,'0');magnitude=b.local(4,'0')
            # SATFINITE maps NaNs to +MAX_NORM and infinities/overflow to
            # sign-preserving MAX_NORM. S2F6 MAX_NORM is 127/64.
            b.body += [
                f'if ({scale.symbol} != 0xff:4) goto <{prefix}_input_classify>;',
                f'goto <{prefix}_nan>;',
                f'<{prefix}_input_classify>',
                f'if ({exp.symbol} != 0xff:4) goto <{prefix}_finite_input>;',
                f'if ({frac.symbol} != 0:4) goto <{prefix}_nan>;',
                f'goto <{prefix}_saturate>;',
                f'<{prefix}_finite_input>',
                # UE8M0 is 2^(scale-127). Divide by it by multiplying by
                # 2^(127-scale), whose F32 exponent field is 254-scale.
                # 2^-127 is subnormal: its encoding is 0x00400000, not an
                # exponent field of zero (which would produce +0).
                f'if ({scale.symbol} == 254:4) goto <{prefix}_scale_254>; ',
                f'{scaled.symbol} = {bits} f* ((254:4 - {scale.symbol}) << 23:4);',
                f'goto <{prefix}_scaled>;','<'+prefix+'_scale_254>',
                f'{scaled.symbol} = {bits} f* 0x00400000:4;',
                f'<{prefix}_scaled>',
                f'if ({scaled.symbol} f< 0xbffe0000:4 || '
                f'{scaled.symbol} f> 0x3ffe0000:4) goto <{prefix}_saturate>;',
                f'{scaled64.symbol} = {scaled.symbol} f* 0x42800000:4;',
                f'{scaled_abs.symbol} = {scaled64.symbol} & 0x7fffffff:4;',
                f'{scaled_exp.symbol} = ({scaled_abs.symbol} >> 23:4) & 0xff:4;',
                f'if ({scaled_exp.symbol} == 0:4) goto <{prefix}_integer_ready>;',
                f'{integer_exp.symbol} = {scaled_exp.symbol} - 127:4;',
                f'if ({integer_exp.symbol} s< 0:4) goto <{prefix}_integer_ready>;',
                f'{significand.symbol} = ({scaled_abs.symbol} & 0x7fffff:4) | 0x800000:4;',
                f'{magnitude.symbol} = {significand.symbol} >> (23:4 - {integer_exp.symbol});',
                f'<{prefix}_integer_ready>',
                f'{trunc_f.symbol} = int2float({magnitude.symbol});',
                f'{residual.symbol} = {scaled_abs.symbol} f- {trunc_f.symbol};',
                f'if ({residual.symbol} f< 0x3f000000:4) goto <{prefix}_rounded>;',
                f'if ({residual.symbol} f> 0x3f000000:4) goto <{prefix}_increment>;',
                f'if (({magnitude.symbol} & 1:4) == 0:4) goto <{prefix}_rounded>;',
                f'<{prefix}_increment>',
                f'{magnitude.symbol} = {magnitude.symbol} + 1:4; goto <{prefix}_rounded>;',
                f'<{prefix}_rounded>',
                f'if (({scaled64.symbol} & 0x80000000:4) == 0:4) goto <{prefix}_positive_result>;',
                f'{out.symbol} = (0:4 - {magnitude.symbol}) & 0xff:4; goto <{prefix}_relu>;',
                f'<{prefix}_positive_result>',
                f'{out.symbol} = {magnitude.symbol} & 0xff:4;',
                f'goto <{prefix}_relu>;',
                f'<{prefix}_saturate>',
                f'if ({sign.symbol} == 0:4) goto <{prefix}_positive_max>;',
                f'{out.symbol} = 0x81:4; goto <{prefix}_relu>;',
                f'<{prefix}_positive_max>',
                f'{out.symbol} = 0x7f:4; goto <{prefix}_relu>;',
                f'<{prefix}_nan>',f'{out.symbol} = 0x7f:4;',
                f'<{prefix}_relu>',
                f'if ({relu} != {relu_value}:4) goto <{prefix}_finish>;',
                f'if (({out.symbol} & 0x80:4) == 0:4) goto <{prefix}_finish>;',
                f'{out.symbol} = 0:4;',
                f'<{prefix}_finish>']
            return out

        if s2f6_f32:
            av=convert_s2f6(b.flag(a,float_value=True),
                f'({scale_word} >> 8:4) & 0xff:4','f2fp_s2f6_a')
            bv=convert_s2f6(b.flag(bb,float_value=True),
                f'{scale_word} & 0xff:4','f2fp_s2f6_b')
        else:
            packed=b.values[pair].symbol
            lo=b.local(4,f'({packed} & 0xffff:4) << 16:4')
            hi=b.local(4,f'{packed} & 0xffff0000:4')
            bv=convert_s2f6(lo.symbol,f'{scale_word} & 0xff:4','f2fp_s2f6_b')
            av=convert_s2f6(hi.symbol,
                f'({scale_word} >> 8:4) & 0xff:4','f2fp_s2f6_a')
        b.body.append(f'{dst.symbol} = (({av.symbol} & 0xff:4) << 8:4) | '
                      f'({bv.symbol} & 0xff:4);')
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
