"""SM89 whole-kernel CUDA/Ghidra semantics oracle.

The GPU and PcodeEmulator execute the same cubin text, including its prologue,
address calculations, memory operations, and EXIT. Threads have independent
emulator states; warp communication and cross-thread shared memory are excluded.
"""
import argparse,ctypes as C,json,math,os,random,re,struct,subprocess,sys,tempfile
from fractions import Fraction
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from sass.cubin import kernel_image
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT

SPECS={
 'iadd3':('IADD3',), 'imad':('IMAD',), 'imad_hi':('IMAD.HI',), 'imad_hi_c':('IMAD.HI',),
 **{f'idp2a_{mode}_{a}{b}':(f'IDP.2A.{mode.upper()}.{"S16" if a=="s" else "U16"}.{"S8" if b=="s" else "U8"}',)
    for mode in ('lo','hi') for a in ('s','u') for b in ('s','u')},
 'lop3':('LOP3',), 'shf':('SHF',), 'iabs':('IABS',), 'imnmx':('IMNMX',),
 'vabsdiff4_s8_merge':('VABSDIFF4',), 'vabsdiff4_u8_merge':('VABSDIFF4',),
 'vabsdiff4_s8_acc':('VABSDIFF4',), 'vabsdiff4_u8_acc':('VABSDIFF4',),
 'dadd':('DADD',), 'dmul':('DMUL',), 'dfma':('DFMA',), 'dsetp':('DSETP',),
 'hadd2':('HADD2',), 'hfma2':('HFMA2',), 'hmul2':('HMUL2',),
 'hmul2_sat':('HMUL2.SAT',), 'hmul2_ftz':('HMUL2.FTZ',), 'hmul2_fmz':('HMUL2.FMZ',), 'hadd2_sat':('HADD2.SAT',),
 'hfma2_sat':('HFMA2.SAT',), 'hfma2_move':('HFMA2.MMA',),
 'hfma2_move_zero':('HFMA2.MMA',), 'hfma2_move_nan':('HFMA2.MMA',),
 'hfma2_move_denorm':('HFMA2.MMA',), 'hfma2_move_inf':('HFMA2.MMA',),
 'hadd2_lanes':('HADD2',), 'hfma2_lanes':('HFMA2',), 'hmul2_lanes':('HMUL2',),
 'prmt':('PRMT',), 'brev':('BREV',), 'popc':('POPC',),
 'bfind':('FLO.U32',), 'bfind_shift':('FLO.U32.SH',),
 'fadd':('FADD',), 'fmul':('FMUL',), 'ffma':('FFMA',),
 'isetp':('ISETP',), 'isetp_ex':('ISETP.GE.U32.AND.EX',), 'sel':('ISETP',), 'fsetp':('FSETP',), 'f2i':('F2I',), 'f2i_abs':('F2I',), 'f2i_neg':('F2I',), 'f2i_absneg':('F2I',), 'f2ip':('F2IP',),
 **{f'i2ip_{fmt}':(f'I2IP.{fmt.upper()}',) for fmt in ('s2','u2','s4','u4','s8','u8','s16','u16')},
 'i2ip_s8_nosat':('I2IP.S8',), 'i2ip_s8_satrelu':('I2IP.S8',),
 'i2ip_s4_satrelu':('I2IP.S4',),
 'fsel':('FSEL',), 'fmin':('FMNMX',), 'fmax':('FMNMX',),
 'fmin_xorsign':('FMNMX.XORSIGN',),
 'fmin_nan':('FMNMX.NAN',), 'fmax_nan':('FMNMX.NAN',),
 'fmin_ftz_nan':('FMNMX.FTZ.NAN',), 'fmax_ftz_nan':('FMNMX.FTZ.NAN',),
 'fmin_ftz':('FMNMX.FTZ',), 'fmax_ftz':('FMNMX.FTZ',),
 'hmnmx2_min':('HMNMX2',), 'hmnmx2_max':('HMNMX2',),
 'hmnmx2_xorsign':('HMNMX2.XORSIGN',),
 'hmnmx2_xorsign_rir':('HMNMX2.XORSIGN',),
 'hmnmx2_xorsign_rir_noabs':('HMNMX2.XORSIGN',),
 'hmnmx2_nan_min':('HMNMX2.NAN',), 'hmnmx2_nan_max':('HMNMX2.NAN',),
 'hmnmx2_ftz_min':('HMNMX2.FTZ',), 'hmnmx2_ftz_max':('HMNMX2.FTZ',),
 'hmnmx2_bf16_min':('HMNMX2.BF16_V2',), 'hmnmx2_bf16_max':('HMNMX2.BF16_V2',),
 'hmnmx2_bf16_nan_min':('HMNMX2.BF16_V2.NAN',), 'hmnmx2_bf16_nan_max':('HMNMX2.BF16_V2.NAN',),
 'hmnmx2_bf16_xorsign':('HMNMX2.BF16_V2.XORSIGN',),
 'hmnmx2_e6m9_min':('HMNMX2.E6M9_V2',), 'hmnmx2_e6m9_max':('HMNMX2.E6M9_V2',),
 'hmnmx2_e6m9_nan_min':('HMNMX2.E6M9_V2.NAN',), 'hmnmx2_e6m9_nan_max':('HMNMX2.E6M9_V2.NAN',),
 'hmnmx2_e6m9_xorsign':('HMNMX2.E6M9_V2.XORSIGN',),
 'i2f':('I2FP',), 'fadd_ftz':('FADD.FTZ',),
 'ffma_sat':('FFMA.SAT',), 'shared':('LDS','STS'), 'local':('LDL','STL'),
 'branch':('BRA','BSSY','BSYNC'), 'ldc':('LDC',),
 **{f'{op}_{mode}':(f'{op.upper()}.{mode.upper()}',) for op in ('fadd','fmul','ffma','dadd','dmul','dfma') for mode in ('rz','rm','rp')}}
ROUNDED={family for family in SPECS if family[-3:] in ('_rz','_rm','_rp')}
SPECS.update({
 'f2fp_f16_pack_rn':('F2FP.F16',), 'f2fp_f16_pack_rz':('F2FP.F16',),
 'f2fp_bf16_pack_rn':('F2FP.BF16',), 'f2fp_bf16_pack_rz':('F2FP.BF16',),
 'f2fp_f16_pack_imm':('F2FP.F16',), 'f2fp_f16_pack_c':('F2FP.F16',),
 'f2fp_f16_pack_relu':('F2FP.RELU.F16',), 'f2fp_f16_pack_satfinite':('F2FP.SATFINITE.F16',),
 'f2fp_f16_pack_relu_satfinite':('F2FP.SATFINITE.RELU.F16',),
 'f2fp_bf16_pack_relu':('F2FP.RELU.BF16',), 'f2fp_bf16_pack_satfinite':('F2FP.SATFINITE.BF16',),
 'f2fp_bf16_pack_relu_satfinite':('F2FP.SATFINITE.RELU.BF16',),
 'f2fp_e4m3_pack':('F2FP.SATFINITE.E4M3',),
 'f2fp_e5m2_pack':('F2FP.SATFINITE.E5M2',),
 'f2fp_e4m3_unpack_h0':('F2FP.F16.E4M3.UNPACK_B',),
 'f2fp_e4m3_unpack_h1':('F2FP.F16.E4M3.UNPACK_B',),
 'f2fp_e5m2_unpack_h0':('F2FP.F16.E5M2.UNPACK_B',),
 'f2fp_e5m2_unpack_h1':('F2FP.F16.E5M2.UNPACK_B',),
 'f2fp_e4m3_unpack_h0_relu':('F2FP.RELU.F16.E4M3.UNPACK_B',),
 'f2fp_e4m3_unpack_h1_relu':('F2FP.RELU.F16.E4M3.UNPACK_B',),
 'f2fp_e5m2_unpack_h0_relu':('F2FP.RELU.F16.E5M2.UNPACK_B',),
 'f2fp_e5m2_unpack_h1_relu':('F2FP.RELU.F16.E5M2.UNPACK_B',),
})
HALF_FLOAT_OUTPUTS={'hadd2','hfma2','hmul2','hmul2_sat','hmul2_ftz','hmul2_fmz','hadd2_sat','hfma2_sat','hadd2_lanes','hfma2_lanes','hmul2_lanes'}
DOUBLE_FAMILIES={'dadd','dmul','dfma','dsetp','isetp_ex'}|{f for f in ROUNDED if f.startswith('d')}
FLOAT_OUTPUTS={'dadd','dmul','dfma','fadd','fmul','ffma','i2f','fadd_ftz','ffma_sat'}|ROUNDED


def known_min_subnormal_bug(family,inputs,emulator,gpu):
    """Identify Ghidra 12.1.4 RN results in its documented half-minsub band."""
    if family not in ('fadd','fmul','ffma','dadd','dmul','dfma'):
        return False
    from fractions import Fraction
    from sass import softfloat
    width=64 if family.startswith('d') else 32
    sign=1<<(width-1)
    if emulator not in (0,sign) or gpu not in (1,sign|1):return False
    parts=[softfloat.unpack(x,width) for x in inputs]
    if any(kind not in ('finite','zero') for kind,_,_ in parts):return False
    if family.endswith('add'):exact=parts[0][2]+parts[1][2]
    elif family.endswith('mul'):exact=parts[0][2]*parts[1][2]
    else:exact=parts[0][2]*parts[1][2]+parts[2][2]
    minimum=Fraction(2)**(-149 if width==32 else -1074)
    if not minimum/2<abs(exact)<minimum:return False
    expected_sign=sign if exact<0 else 0
    return emulator==expected_sign and gpu==(expected_sign|1)


def fp8_reference(bits,fmt):
    """Independent nearest-even finite FP8 encoding from exact rational values."""
    from sass import softfloat
    kind,sign,value=softfloat.unpack(bits,32);signbit=sign<<7
    if kind=='nan':return signbit|0x7f
    if kind=='inf':return signbit|(0x7b if fmt=='E5M2' else 0x7e)
    mbits=2 if fmt=='E5M2' else 3
    bias=15 if fmt=='E5M2' else 7
    value=abs(value);candidates=[]
    for code in range(128):
        exp=code>>mbits;mant=code&((1<<mbits)-1)
        if fmt=='E5M2' and exp==31:continue
        if fmt=='E4M3' and exp==15 and mant==7:continue
        if exp==0:v=Fraction(mant)*(Fraction(2)**(1-bias-mbits))
        else:v=Fraction((1<<mbits)+mant)*(Fraction(2)**(exp-bias-mbits))
        candidates.append((code,v))
    if value>=candidates[-1][1]:return signbit|candidates[-1][0]
    distance=min(abs(value-v) for _,v in candidates)
    ties=[code for code,v in candidates if abs(value-v)==distance]
    return signbit|(ties[0] if len(ties)==1 else next(code for code in ties if not (code&1)))


def fp8_to_f16_reference(code,fmt):
    sign=(code&0x80)<<8
    if fmt=='E5M2':
        if (code&0x7c)==0x7c and (code&3):return 0x7fff
        return code<<8
    exp=(code>>3)&15;mant=code&7
    if exp==15 and mant==7:return 0x7fff
    if exp==0:
        if mant==0:return sign
        if mant&4:return sign|(8<<10)|((mant&3)<<8)
        if mant&2:return sign|(7<<10)|((mant&1)<<9)
        return sign|(6<<10)
    return sign|((exp+8)<<10)|(mant<<7)



def kernel_request(cubin, family, data):
    image=kernel_image(cubin,'sem_'+family)
    if image['arch']!='SM89':raise ValueError('SM89 cubin required')
    expected_parameters=([8,8,8] if family=='imad_hi_c' else
                         [8,8,4] if family=='f2fp_f16_pack_c' else [8,8])
    if [p['size'] for p in image['parameters']] != expected_parameters:
        raise ValueError('unexpected GPU oracle kernel parameter layout')
    input_size=8 if family in DOUBLE_FAMILIES else 4
    output_size=8 if family in DOUBLE_FAMILIES or family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')) else 4
    count=len(data)//(3*input_size)
    if not count or len(data)%(3*input_size) or count%32:raise ValueError('input must contain whole 32-thread blocks')
    input_address,output_address=0x100000000,0x200000000
    constants=dict(image['constants']);bank=bytearray(constants[0])
    # CUDA launch dimensions and local stack base, before the metadata-defined
    # parameter region. These offsets are confirmed from SM89 compiler output.
    struct.pack_into('<III',bank,0,32,1,1)
    struct.pack_into('<I',bank,0x28,0x1000)
    arguments=((input_address,output_address,0x00000002ffffffff) if family=='imad_hi_c' else
               (input_address,output_address,0x3fc00000) if family=='f2fp_f16_pack_c' else
               (input_address,output_address))
    for parameter,value in zip(image['parameters'],arguments):
        fmt='<Q' if parameter['size']==8 else '<I'
        struct.pack_into(fmt,bank,image['parameter_base']+parameter['offset'],value)
    constants[0]=bytes(bank)
    memory=[dict(space='cbank',address=index<<32,hex=value.hex()) for index,value in constants.items()]
    memory += [dict(space='ram',address=input_address,hex=data.hex()),
               dict(space='ram',address=output_address,hex=('cc'*count*output_size))]
    return dict(kernel=dict(name=image['name'],code=image['code'].hex(),threads=count,block_size=32),
                memory=memory,output=dict(address=output_address,size=output_size,stride=output_size),
                context=dict(synchronization=dict(bssy=True,bsync=True)))

def patch_half_fixtures(cubin):
    """Preserve compiled prologues and scheduling; change only arithmetic fields.

    These fixtures test SASS modes directly rather than relying on PTX lowering
    to choose a lane broadcast or the MMA constant-move encoding.
    """
    from sass import cubin as elf,ir,pydecode
    from semantic_cases import encode
    data=bytearray(cubin.read_bytes());image=elf.sections(data)
    shoff,=struct.unpack_from('<Q',data,0x28)
    entsize,num,strndx=struct.unpack_from('<HHH',data,0x3a)
    headers=[struct.unpack_from('<IIQQQQIIQQ',data,shoff+i*entsize) for i in range(num)]
    strings=headers[strndx][4]
    offsets={bytes(data[strings+h[0]:data.index(0,strings+h[0])]).decode():h[4] for h in headers}
    changes={
        'hfma2_move':('HFMA2','hfma2_mma__RRI_norelu',dict(Ra=255,Rb=255,**{'Ra@negate':1,'Rb@negate':0})),
        'hadd2_lanes':('HADD2',None,dict(iswzA='H0_H0',iswzB_as_C='H1_H1',**{'Ra@absolute':1,'Rc@negate':1})),
        'hfma2_lanes':('HFMA2',None,dict(iswzA='H1_H1',iswzB='H0_H0',iswzC='H1_H0',**{'Ra@negate':1,'Rb@absolute':1})),
        'hmul2_lanes':('HMUL2',None,dict(iswzA='H0_H0',iswzB='H1_H1',**{'Ra@absolute':1,'Rb@negate':1})),
        'hmul2_fmz':('HMUL2','hmul2__RR',dict(fmz='FMZ')),
        'fmin_nan':('FMNMX',None,dict(nan='NAN')),
        'fmax_nan':('FMNMX',None,dict(nan='NAN')),
        'fmin_ftz_nan':('FMNMX',None,dict(ftz='FTZ',nan='NAN')),
        'fmax_ftz_nan':('FMNMX',None,dict(ftz='FTZ',nan='NAN'))}
    changes.update({
        'hmnmx2_xorsign':('HMNMX2','hmnmx2__RRR',dict(xorsign='XORSIGN',
            **{'Ra@absolute':1,'Rb@absolute':1})),
        'hmnmx2_xorsign_rir':('HMNMX2','hmnmx2__RIR',dict(xorsign='XORSIGN',
            Sb=0x3c00,Sb1=0x3c00,**{'Ra@absolute':1})),
        'hmnmx2_xorsign_rir_noabs':('HMNMX2','hmnmx2__RIR',dict(xorsign='XORSIGN',
            Sb=0x3c00,Sb1=0x3c00,**{'Ra@absolute':0})),
        'hmnmx2_nan_min':('HMNMX2','hmnmx2__RRR',dict(nan='NAN')),
        'hmnmx2_nan_max':('HMNMX2','hmnmx2__RRR',dict(nan='NAN')),
        'hmnmx2_ftz_min':('HMNMX2','hmnmx2__RRR',dict(ftz='FTZ')),
        'hmnmx2_ftz_max':('HMNMX2','hmnmx2__RRR',dict(ftz='FTZ')),
        'hmnmx2_bf16_min':('HMNMX2','hmnmx2__RRR',dict(ofmt='BF16_V2')),
        'hmnmx2_bf16_max':('HMNMX2','hmnmx2__RRR',dict(ofmt='BF16_V2')),
        'hmnmx2_bf16_nan_min':('HMNMX2','hmnmx2__RRR',dict(ofmt='BF16_V2',nan='NAN')),
        'hmnmx2_bf16_nan_max':('HMNMX2','hmnmx2__RRR',dict(ofmt='BF16_V2',nan='NAN')),
        'hmnmx2_bf16_xorsign':('HMNMX2','hmnmx2__RRR',dict(ofmt='BF16_V2',xorsign='XORSIGN',
            **{'Ra@absolute':1,'Rb@absolute':1})),
        'hmnmx2_e6m9_min':('HMNMX2','hmnmx2__RRR',dict(ofmt='E6M9_V2')),
        'hmnmx2_e6m9_max':('HMNMX2','hmnmx2__RRR',dict(ofmt='E6M9_V2')),
        'hmnmx2_e6m9_nan_min':('HMNMX2','hmnmx2__RRR',dict(ofmt='E6M9_V2',nan='NAN')),
        'hmnmx2_e6m9_nan_max':('HMNMX2','hmnmx2__RRR',dict(ofmt='E6M9_V2',nan='NAN')),
        'hmnmx2_e6m9_xorsign':('HMNMX2','hmnmx2__RRR',dict(ofmt='E6M9_V2',xorsign='XORSIGN',
            **{'Ra@absolute':1,'Rb@absolute':1}))})
    changes.update({
        'i2ip_s8_nosat':('I2IP','i2ip__RRR_RRR',dict(satrelu='nosatrelu')),
        'i2ip_s8_satrelu':('I2IP','i2ip__RRR_RRR',dict(satrelu='SATRELU')),
        'i2ip_s4_satrelu':('I2IP','i2ip_24__RRR_RRR',dict(satrelu='SATRELU'))})
    changes['imad_hi_c']=('IMAD','imad_hi__RRC_RRC',dict(Sc_bank=0,Sc_addr=0x170))
    for family,absolute,negate in (('f2i_abs',1,0),('f2i_neg',0,1),('f2i_absneg',1,1)):
        changes[family]=('F2I','f2i__Rb_32b',{'Rb@absolute':absolute,'Rb@negate':negate})
    for family,fmt in (('f2fp_e4m3_pack','E4M3'),('f2fp_e5m2_pack','E5M2')):
        changes[family]=('F2FP','f2fp_f32_to_8b_downconvert__RRR',dict(
            dstfmt=fmt,srcfmt='F32',merge='PACK_AB_MERGE_C',rndMode='RN',
            satfinite='SATFINITE',relu='norelu',extract='H1'))
    for family in ('f2fp_e4m3_unpack_h0_relu','f2fp_e4m3_unpack_h1_relu',
                   'f2fp_e5m2_unpack_h0_relu','f2fp_e5m2_unpack_h1_relu'):
        changes[family]=('F2FP',None,{})
    for family,fmt,red in (
        ('vabsdiff4_s8_merge','S8','SIMD_MRG'),('vabsdiff4_u8_merge','U8','SIMD_MRG'),
        ('vabsdiff4_s8_acc','S8','ACC'),('vabsdiff4_u8_acc','U8','ACC')):
        changes[family]=('LOP3','vabsdiff4__RRR_RRR',dict(Pu='PT',fmt=fmt,red=red))
    for suffix,bits in [('zero',0x80000000),('nan',0xfe01fd01),('denorm',0x000103ff),('inf',0x7c00fc00)]:
        changes['hfma2_move_'+suffix]=('HFMA2','hfma2_mma__RRI_norelu',
            dict(Ra=255,Rb=255,Sc=bits>>16,Sc1=bits&0xffff,**{'Ra@negate':1,'Rb@negate':0}))
    for family,(mnemonic,classname,fields) in changes.items():
        section='.text.sem_'+family;code=image[section];matches=[]
        for offset in range(0,len(code),16):
            decoded=pydecode.decode('SM89',code[offset:offset+16])
            if decoded.klass.mnemonic==mnemonic and (family!='imad_hi_c' or decoded.klass.name=='imad_hi__RRR_RRR'):
                matches.append((offset,decoded))
        is_unpack_relu=family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')) and family.endswith('_relu')
        expected_matches=2 if is_unpack_relu else 1
        if len(matches)!=expected_matches:
            raise ValueError(f'{family}: expected {expected_matches} {mnemonic} instructions, got {len(matches)}')
        if is_unpack_relu:
            for offset,decoded in matches:
                old=int.from_bytes(code[offset:offset+16],'little')
                replacement=(old^(1<<75)).to_bytes(16,'little')
                check=pydecode.decode('SM89',replacement)
                relu_enum=ir.load('SM89').enums[decoded.klass.operand_types['relu'].type]
                if check.env.get('relu')!=relu_enum['RELU']:
                    raise ValueError(f'{family}: patched selector did not decode as RELU: {check.text}')
                data[offsets[section]+offset:offsets[section]+offset+16]=replacement
            continue
        offset,decoded=matches[0]
        target=next(k for k in ir.load('SM89').classes if k.name==(classname or decoded.klass.name))
        names=target.operand_types
        values={n:v for n,v in decoded.env.items() if isinstance(n,str) and n in names}
        values.update({('@'.join(n)):v for n,v in decoded.env.items() if isinstance(n,tuple) and len(n)==2 and n[0] in names})
        values.update(fields)
        if family.startswith('f2fp_e') and family.endswith('_pack'):
            # The compiler's third input is the last LDG before this F2FP.
            # Hardware carries the selected C half when Rd aliases Rc.
            loads=[pydecode.decode('SM89',code[i:i+16])
                   for i in range(0,offset,16)]
            c_regs=[d.env['Rd'] for d in loads
                    if d.klass.mnemonic.startswith('LDG') and 'Rd' in d.env]
            if len(c_regs)<3:raise ValueError(f'{family}: expected three input loads')
            values['Rc']=c_regs[-1]
            values['Rd']=c_regs[-1]
        replacement=int.from_bytes(bytes.fromhex(encode('SM89',classname or decoded.klass.name,**values)),'little')
        old=int.from_bytes(code[offset:offset+16],'little');mask=(1<<105)-1
        data[offsets[section]+offset:offsets[section]+offset+16]=((replacement&mask)|(old&~mask)).to_bytes(16,'little')
        if family.startswith('f2fp_e') and family.endswith('_pack'):
            # The PTX kernel normally extracts the 16-bit result and rebuilds
            # the upper half from its original C value. Route that mask from
            # F2FP's full SASS result instead, exposing PACK_AB_MERGE_C.
            rd=values['Rd']
            selectors=[]
            for candidate in range(offset+16,len(code),16):
                d=pydecode.decode('SM89',code[candidate:candidate+16])
                if d.klass.mnemonic=='LOP3' and d.env.get('Sb')==0xffff0000:
                    selectors.append((candidate,d))
                elif d.klass.mnemonic=='LOP3' and d.env.get('Sb')==0xffff:
                    selectors.append((candidate,d))
            if len(selectors)!=2:raise ValueError(f'{family}: expected two output half masks, got {len(selectors)}')
            for candidate,d in selectors:
                operands={n:v for n,v in d.env.items() if isinstance(n,str) and n in d.klass.operand_types}
                operands.update({'@'.join(n):v for n,v in d.env.items()
                                 if isinstance(n,tuple) and len(n)==2 and n[0] in d.klass.operand_types})
                if d.env['Sb']==0xffff0000:operands['Ra']=rd
                else:operands['Rc']=rd
                replacement=int.from_bytes(bytes.fromhex(encode('SM89',d.klass.name,**operands)),'little')
                old=int.from_bytes(code[candidate:candidate+16],'little')
                data[offsets[section]+candidate:candidate+16+offsets[section]]= (
                    (replacement&mask)|(old&~mask)).to_bytes(16,'little')
    cubin.write_bytes(data)
    return sorted(changes)

class Driver:
    def __init__(self):
        self.lib=C.CDLL('libcuda.so.1');self.ctx=C.c_void_p();self.allocations=[];self.module=C.c_void_p()
        self.call('cuInit',0);device=C.c_int();self.call('cuDeviceGet',C.byref(device),0)
        major,minor=C.c_int(),C.c_int()
        self.call('cuDeviceGetAttribute',C.byref(major),75,device);self.call('cuDeviceGetAttribute',C.byref(minor),76,device)
        if (major.value,minor.value)!=(8,9):raise RuntimeError('SM89 GPU required, found '+str((major.value,minor.value)))
        self.call('cuCtxCreate_v2',C.byref(self.ctx),0,device)
    def call(self,name,*args):
        rc=getattr(self.lib,name)(*args)
        if rc:raise RuntimeError(f'{name}: CUDA status {rc}')
    def malloc(self,n):
        ptr=C.c_uint64();self.call('cuMemAlloc_v2',C.byref(ptr),C.c_size_t(n));self.allocations.append(ptr);return ptr
    def close(self):
        for ptr in self.allocations:self.call('cuMemFree_v2',ptr)
        if self.module:self.call('cuModuleUnload',self.module)
        if self.ctx:self.call('cuCtxDestroy_v2',self.ctx)
    def run(self,cubin,family,data):
        self.call('cuModuleLoad',C.byref(self.module),C.c_char_p(os.fsencode(cubin)))
        fn=C.c_void_p();self.call('cuModuleGetFunction',C.byref(fn),self.module,C.c_char_p(('sem_'+family).encode()))
        input_size=8 if family in DOUBLE_FAMILIES else 4
        output_size=8 if family in DOUBLE_FAMILIES or family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')) else 4
        a=self.malloc(len(data));n=len(data)//(3*input_size);b=self.malloc(n*output_size)
        inp=C.create_string_buffer(data)
        self.call('cuMemcpyHtoD_v2',a,inp,C.c_size_t(len(data)))
        if family=='imad_hi_c':
            addend=C.c_uint64(0x00000002ffffffff)
            params=(C.c_void_p*3)(C.cast(C.byref(a),C.c_void_p),C.cast(C.byref(b),C.c_void_p),C.cast(C.byref(addend),C.c_void_p))
        elif family=='f2fp_f16_pack_c':
            source=C.c_uint32(0x3fc00000)
            params=(C.c_void_p*3)(C.cast(C.byref(a),C.c_void_p),C.cast(C.byref(b),C.c_void_p),C.cast(C.byref(source),C.c_void_p))
        else:
            params=(C.c_void_p*2)(C.cast(C.byref(a),C.c_void_p),C.cast(C.byref(b),C.c_void_p))
        self.call('cuLaunchKernel',fn,C.c_uint(n//32),C.c_uint(1),C.c_uint(1),C.c_uint(32),C.c_uint(1),C.c_uint(1),C.c_uint(0),C.c_void_p(),params,C.c_void_p())
        result=C.create_string_buffer(n*output_size)
        self.call('cuCtxSynchronize');self.call('cuMemcpyDtoH_v2',result,b,C.c_size_t(n*output_size))
        self.call('cuModuleUnload',self.module);self.module=C.c_void_p()
        return struct.unpack('<'+('Q' if output_size==8 else 'I')*n,result.raw)

def main():
    p=argparse.ArgumentParser();p.add_argument('--compile-only',action='store_true');p.add_argument('--require-gpu',action='store_true');p.add_argument('--output',default='/tmp/sass-gpu-oracle');a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True);cubin=out/'scalar_semantics.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler','-diag-suppress=177','-cubin','-arch=sm_89',str(ROOT/'tests/kernels/scalar_semantics.cu'),'-o',str(cubin)],check=True)
    patched=patch_half_fixtures(cubin)
    sass=subprocess.check_output([os.environ.get('CUOBJDUMP','/usr/local/cuda/bin/cuobjdump'),'-sass',str(cubin)],text=True);(out/'scalar_semantics.sass').write_text(sass)
    for family,mnemonics in SPECS.items():
        match=re.search(r'Function : sem_'+family+r'\b(.*?)(?=Function :|\Z)',sass,re.S)
        if family.startswith('hmnmx2_e6m9'):
            from sass import pydecode
            code=kernel_image(cubin.read_bytes(),'sem_'+family)['code']
            decoded=[pydecode.decode('SM89',code[i:i+16]) for i in range(0,len(code),16)]
            if not any(any(d.text.startswith(mnemonic) for mnemonic in mnemonics) for d in decoded):
                raise RuntimeError(f'{family}: CuLifter decoder did not confirm the E6M9 SASS selector')
            continue
        for mnemonic in mnemonics:
            if not match or not re.search(r'\b'+re.escape(mnemonic)+r'(?:\.|\s)',match[1]):raise RuntimeError('fixture does not contain '+mnemonic+' for '+family)
        if family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')):
            ops=re.findall(r'F2FP\.(?:RELU\.)?F16\.(?:E4M3|E5M2)\.UNPACK_B\s+([^;]+);',match[1])
            if len(ops)!=2 or sum('.H1' in op for op in ops)!=1:
                raise RuntimeError(f'{family}: expected one full-word H0 and one H1 conversion, got {ops}')
    print(f'Compiled and confirmed {len(SPECS)} SM89 kernel fixtures',flush=True)
    if a.compile_only:return
    try:driver=Driver()
    except (OSError,RuntimeError) as e:
        if a.require_gpu:raise
        print('SKIP GPU execution: '+str(e));return
    try:
        rng=random.Random(1);fixtures=[];requests=[];results=[]
        for family in SPECS:
            if family=='imad_hi_c':
                local_rng=random.Random(0x1a2ad)
                vectors=[[local_rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
            elif family.startswith('idp2a_'):
                # New families must not shift the legacy oracle vectors.
                local_rng=random.Random(sum((i+1)*ord(ch) for i,ch in enumerate(family)))
                vectors=[[local_rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
            else:
                vectors=[[rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
            edges=[0,0xffffffff,0x80000000,0x7fffffff,0x7f800000,0xff800000,0x7fc00000,1,
                   0x007fffff,0x00800000,0x3f800000,0xbf800000,0x7f7fffff,0x80000001]
            vectors[:len(edges)]=[[x,x,x] for x in edges]
            if family.startswith('vabsdiff4_'):
                vectors[:8]=[(0,0xffffffff,0xdeadbeef),
                    (0x807f01ff,0xff010080,0x7f80ff01),
                    (0x80808080,0x7f7f7f7f,0x01020304),
                    (0xffffffff,0,0x80000000),
                    (0x7f000180,0x80ff017f,0xffffffff),
                    (0x01020304,0x05060708,0x00000000),
                    (0x80000000,0x7fffffff,0x7f80ff01),
                    (0xdeadbeef,0x12345678,0xabcdef01)]
            if family=='imad_hi_c':
                vectors[:8]=[(0xffffffff,2,0),(0x80000000,2,0),(0xffffffff,0xffffffff,0),
                    (0x80000001,0xffffffff,0),(0x7fffffff,0x7fffffff,0),
                    (0x12345678,0x87654321,0),(0x40000000,4,0),(0xfffffffe,3,0)]
            if family.startswith('idp2a_'):
                vectors[:8]=[(0x80017fff,0x807f01ff,0x7fffffff),
                    (0x7fff8001,0x01020304,0x80000000),(0xffff0001,0x807f80ff,0xffffffff),
                    (0x00010001,0xffffffff,0),(0x80008000,0x7f807f80,1),
                    (0x7fff7fff,0x008000ff,0xfffffffe),(0xffff7fff,0x80ff00ff,0x12345678),
                    (0x01020304,0x05060708,0x87654321)]
            if family in ('fsel','fmin','fmax','fmin_nan','fmax_nan','fmin_ftz_nan','fmax_ftz_nan','fmin_ftz','fmax_ftz','fmin_xorsign'):
                pairs=[(0,0x80000000),(0x80000000,0),(0x7fc00000,0x3f800000),
                       (0x3f800000,0x7fc00000),(0x7f800001,0xff800001),
                       (1,0),(0x80000001,0),(0x007fffff,0x80000001)]
                vectors[len(edges):len(edges)+2*len(pairs)]=[[x,y,p] for x,y in pairs for p in (0,1)]
            if family.startswith('hmnmx2_'):
                if family.startswith('hmnmx2_bf16'):
                    pairs=[(0x3f803f80,0xc000c000),(0x00000000,0x80008000),
                           (0x7fc17f80,0x40003f80),(0x00010001,0x80018001),
                           (0x7f80ff80,0x7f7fff7f),(0xbf803f80,0x4200c000),
                           (0xffc17fc1,0x7fc17fc1)]
                elif family.startswith('hmnmx2_e6m9'):
                    pairs=[(0x40004000,0x3f803f80),(0x00000000,0x80008000),
                           (0x7fff7fff,0x3f803f80),(0x7e00fe00,0x3f80bf80),
                           (0x00010001,0x80028002),(0xc0003f80,0x41003e00),
                           (0xffff7fff,0x7fff7fff)]
                elif family.startswith('hmnmx2_xorsign_rir'):
                    pairs=[(0xc000c000,0),(0x40004000,0),(0x3c003c00,0),
                           (0xbc00bc00,0),(0xc0003c00,0),(0x3c00c000,0)]
                else:
                    pairs=[(0x3c003c00,0xc000c000),(0x00000000,0x80008000),
                           (0x7e017c00,0x40003c00),(0x00010001,0x80018001),
                           (0x7c00fc00,0x7bfffbff),(0xbc003c00,0x4200c000),
                           (0xffc17fc1,0x7fc17fc1)]
                if family=='hmnmx2_xorsign':
                    pairs += [(0x80017e01,0x7e017e01),(0x7e017e01,0x80017e01)]
                elif family=='hmnmx2_bf16_xorsign':
                    pairs += [(0x80010000,0x7fc17fc1),(0x7fc17fc1,0x80010000)]
                elif family=='hmnmx2_e6m9_xorsign':
                    pairs += [(0x80010000,0x7e017e01),(0x7e017e01,0x80010000)]
                vectors=[[a,b,0] for a,b in (pairs*(128//len(pairs)+1))[:128]]
            if family in HALF_FLOAT_OUTPUTS or family=='hfma2_move':
                edges16=[0,0x8000,1,0x8001,0x03ff,0x0400,0x3c00,0xbc00,0x7bff,0xfbff,0x7c00,0xfc00,0x7e01]
                vectors[:len(edges16)]=[[x|(edges16[(i+3)%len(edges16)]<<16),
                    edges16[(i+5)%len(edges16)]|(x<<16),
                    edges16[(i+7)%len(edges16)]|(edges16[(i+9)%len(edges16)]<<16)] for i,x in enumerate(edges16)]
            if family.startswith('hmul2'):
                pairs=[(0x00010001,0x60006000),(0x80018001,0x60006000),
                       (0x00010001,0xe000e000),(0x80018001,0xe000e000),
                       (0x14001400,0x14001400),(0x94009400,0x14001400),
                       (0x94009400,0x94009400),(0x40004000,0x40004000),
                       (0xbc00bc00,0x40004000),(0x7e017c00,0x3c003c00),
                       (0x7c007e01,0x3c003c00),(0x7c007c00,0x00000000)]
                vectors[:len(pairs)]=[[x,y,0] for x,y in pairs]
            if family=='f2ip':
                values=[0,0x80000000,0x3f000000,0xbf000000,0x3fc00000,0xbfc00000,
                        0x40200000,0xc0200000,0x42ff0000,0xc3000000,0x43000000,
                        0xc2ff0000,0x42ff8000,0xc2ff8000,0x437f8000,0x7f800000,
                        0xff800000,0x7fc00000,0x7f800001,0x4f000000,0xcf000000,
                        0x7f7fffff,0xff7fffff]
                vectors=[[values[i%len(values)],values[(i+7)%len(values)],
                          rng.getrandbits(32)] for i in range(128)]
            if family.startswith(('f2fp_f16_pack_','f2fp_bf16_pack_')):
                values=[0,0x80000000,0x3f800000,0xbf800000,0x3f801000,
                    0x3f803000,0x33000000,0x33800000,0x387fc000,0x38800000,
                    0x477fe000,0x477ff000,0x7f800000,0xff800000,0x7fc00000,
                    0x7f800001,1,0x007fffff,0x00800000,0x7f7fffff,0xff7fffff]
                vectors=[[values[i%len(values)],values[(i*7+3)%len(values)],0]
                         for i in range(128)]
            if family in ('f2fp_e4m3_pack','f2fp_e5m2_pack'):
                values=[0,0x80000000,1,0x007fffff,0x00800000,0x33800000,
                    0x3f800000,0xbf800000,0x3f801000,0x3f803000,0x40600000,
                    0x43e00000,0x43e10000,0x43e08000,0x477fe000,0x7f800000,
                    0xff800000,0x7fc00000,0x7f800001]
                vectors=[[values[i%len(values)],values[(i*7+3)%len(values)],
                          (0x13579bdf ^ (i*0x01010101))&0xffffffff]
                         for i in range(128)]
            if family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')):
                extract='H1' if '_h1' in family else 'H0'
                vectors=[]
                for i in range(128):
                    pair=i|((255-i)<<8)
                    vectors.append([pair if extract=='H0' else pair<<16,0,0])
            if family.startswith('i2ip_'):
                match=re.fullmatch(r'i2ip_(s2|u2|s4|u4|s8|u8|s16|u16)(?:_(nosat|satrelu))?',family)
                if not match:raise ValueError(f'bad I2IP fixture name {family}')
                fmt,mode=match.groups();width=int(fmt[1:]);signed=fmt.startswith('s')
                lo=-(1<<(width-1)) if signed else 0
                hi=(1<<(width-1))-1 if signed else (1<<width)-1
                edges=[0,1,0xffffffff,lo-1,lo,hi,hi+1,0x12345678,0x89abcdef]
                edges=[x&0xffffffff for x in edges]
                vectors=[[edges[i%len(edges)],edges[(i+4)%len(edges)],
                          0x89abcdef^(i*0x01010101)] for i in range(128)]
            if family in DOUBLE_FAMILIES:
                vectors=[[rng.getrandbits(64) for _ in range(3)] for _ in range(128)]
                edges64=[0,1<<63,1,(1<<63)|1,0x000fffffffffffff,0x0010000000000000,
                         0x3ff0000000000000,0xbff0000000000000,0x7fefffffffffffff,
                         0x7ff0000000000000,0xfff0000000000000,0x7ff8000000000000]
                vectors[:len(edges64)]=[[x,x,x] for x in edges64]
                pairs=[(0,1<<63,0),(1<<63,0,0),(0x7ff8000000000000,0,0),
                       (0,0x7ff0000000000001,0),
                       (0x3ff0000000000001,0x3feffffffffffffe,0xbff0000000000000),
                       (0x7fefffffffffffff,0x4000000000000000,0xffefffffffffffff)]
                vectors[len(edges64):len(edges64)+len(pairs)]=pairs
                if family=='isetp_ex':
                    pairs=[(0,1,0),(1,0,0),(0,0,0),(1,1,0),
                           (0x00000001ffffffff,0x0000000200000000,0),
                           (0x0000000200000000,0x00000001ffffffff,0),
                           (0x7fffffffffffffff,0x8000000000000000,0),
                           (0x8000000000000000,0x7fffffffffffffff,0),
                           (0xffffffffffffffff,0,0),(0,0xffffffffffffffff,0)]
                    vectors[:len(pairs)]=pairs
            if family in ROUNDED:
                # Residuals below the wide format's precision, exact cancellation
                # (zero sign) and overflow, per format.
                one,tiny,top=(0x3ff0000000000000,1,0x7fefffffffffffff) if family in DOUBLE_FAMILIES else (0x3f800000,1,0x7f7fffff)
                sign=1<<(63 if family in DOUBLE_FAMILIES else 31)
                pairs=[(one,tiny,one|sign),(one,tiny|sign,tiny),(one,one|sign,one),(one|sign,one,tiny|sign),
                       (top,top,top|sign),(top|sign,top,top),(one+1,one+1,tiny|sign),(0,sign,0)]
                vectors[64:64+len(pairs)]=[list(v) for v in pairs]
            data=b''.join(struct.pack('<QQQ' if family in DOUBLE_FAMILIES else '<III',*v) for v in vectors)
            gpu=driver.run(cubin,family,data)
            fixtures.append((family,vectors,gpu));requests.append(kernel_request(cubin.read_bytes(),family,data))
        process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),'SASS:LE:64:sm89'],
            input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True)
        emulated=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if len(emulated)!=len(fixtures):raise RuntimeError(process.stderr[-2000:])
        def nan(x,width):
            exponent,mantissa=(0x7ff0000000000000,0xfffffffffffff) if width==64 else (0x7f800000,0x7fffff)
            return x&exponent==exponent and x&mantissa!=0
        for (family,vectors,gpu),result in zip(fixtures,emulated):
            if 'error' in result:raise RuntimeError(f'{family}: {result["error"]}')
            if family=='isetp_ex':
                expected=tuple(int(x<y) for x,y,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],gpu[mismatch],expected[mismatch]))
            if family=='imad_hi_c':
                # This class's 64-bit constant source participates in the
                # full product-plus-addend sum before high-word extraction.
                expected=tuple(((x*y+0x00000002ffffffff)>>32)&0xffffffff
                               for x,y,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            body=re.search(r'Function : sem_'+family+r'\b(.*?)(?=Function :|\Z)',sass,re.S)[1]
            instructions={int(offset,16):mnemonic for offset,mnemonic in re.findall(
                r'/\*([0-9a-f]+)\*/\s+(?:@!?\w+\s+)?([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)',body)}
            if family.startswith('hmnmx2_e6m9'):
                from sass import pydecode
                code=kernel_image(cubin.read_bytes(),'sem_'+family)['code']
                for offset in range(0,len(code),16):
                    decoded=pydecode.decode('SM89',code[offset:offset+16])
                    if decoded.klass.mnemonic=='HMNMX2':
                        instructions[offset]=decoded.text.split()[0]
            executed={instructions[offset] for offset in result['visited']}
            for target in SPECS[family]:
                if not any(op==target or op.startswith(target+'.') for op in executed):
                    raise AssertionError(f'{family}: target {target} was never executed')
            actual=[int.from_bytes(bytes.fromhex(value),'little') for value in result['outputs']]
            if len(actual)!=len(gpu):raise RuntimeError(f'{family}: missing emulated outputs')
            excluded=[]
            if family in ('brev','popc','bfind','bfind_shift'):
                def reference(x):
                    if family=='popc':return x.bit_count()
                    if family=='brev':return sum(((x>>bit)&1)<<(31-bit) for bit in range(32))
                    if x==0:return 0xffffffff
                    index=x.bit_length()-1
                    return 31-index if family=='bfind_shift' else index
                expected=tuple(reference(v[0]) for v in vectors)
                if gpu!=expected:
                    mismatch=next((i for i,(actual,want) in enumerate(zip(gpu,expected)) if actual!=want),None)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family in ('f2i','f2i_abs','f2i_neg','f2i_absneg'):
                absolute=family in ('f2i_abs','f2i_absneg')
                negate=family in ('f2i_neg','f2i_absneg')
                def cvt_i32(bits):
                    if absolute:bits &= 0x7fffffff
                    if negate:bits ^= 0x80000000
                    sign=bits>>31;exp=(bits>>23)&255;frac=bits&0x7fffff
                    if exp==255:
                        if frac:return 0
                        return 0x80000000 if sign else 0x7fffffff
                    if exp==0:return 0
                    e=exp-127
                    if e<0:return 0
                    if e>=31:return 0x80000000 if sign else 0x7fffffff
                    magnitude=((frac|0x800000) << (e-23) if e>=23 else
                               (frac|0x800000) >> (23-e))
                    return (-magnitude if sign else magnitude)&0xffffffff
                expected=tuple(cvt_i32(x) for x,_,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family in ('fmin','fmax','fmin_nan','fmax_nan','fmin_ftz_nan','fmax_ftz_nan','fmin_ftz','fmax_ftz'):
                is_min=family.startswith('fmin')
                is_nan_mode=family.endswith('_nan')
                is_ftz='ftz' in family
                def fmnmx_expected(x,y):
                    if is_ftz:
                        if x&0x7f800000==0:x &= 0x80000000
                        if y&0x7f800000==0:y &= 0x80000000
                    xnan=x&0x7f800000==0x7f800000 and x&0x007fffff!=0
                    ynan=y&0x7f800000==0x7f800000 and y&0x007fffff!=0
                    if is_nan_mode and (xnan or ynan):return 0x7fffffff
                    if xnan:return 0x7fffffff if ynan else y
                    if ynan:return x
                    xf=struct.unpack('<f',x.to_bytes(4,'little'))[0]
                    yf=struct.unpack('<f',y.to_bytes(4,'little'))[0]
                    if xf==yf==0:return (x|y) if is_min else (x&y)
                    return x if (xf<yf)==is_min else y
                expected=tuple(fmnmx_expected(x,y) for x,y,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family=='fmin_xorsign':
                def xorsign_min(x,y):
                    sign=(x^y)&0x80000000
                    x&=0x7fffffff;y&=0x7fffffff
                    xnan=x&0x7f800000==0x7f800000 and x&0x007fffff!=0
                    ynan=y&0x7f800000==0x7f800000 and y&0x007fffff!=0
                    if xnan:return 0x7fffffff if ynan else y|sign
                    if ynan:return x|sign
                    xf=struct.unpack('<f',x.to_bytes(4,'little'))[0]
                    yf=struct.unpack('<f',y.to_bytes(4,'little'))[0]
                    return (x if xf<yf else y)|sign
                expected=tuple(xorsign_min(x,y) for x,y,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith('hmnmx2_'):
                is_min=family.endswith('_min') or family.startswith(('hmnmx2_xorsign','hmnmx2_bf16_xorsign','hmnmx2_e6m9_xorsign'))
                nan_mode='_nan_' in family;ftz='_ftz_' in family
                xorsign=family.startswith(('hmnmx2_xorsign','hmnmx2_bf16_xorsign','hmnmx2_e6m9_xorsign'))
                bf16=family.startswith('hmnmx2_bf16')
                e6m9=family.startswith('hmnmx2_e6m9')
                def half_minmax(a,b):
                    rir=family.startswith('hmnmx2_xorsign_rir')
                    if rir:b=0x3c003c00
                    result=0
                    for shift in (0,16):
                        x=(a>>shift)&0xffff;y=(b>>shift)&0xffff
                        sx=(x>>15)&1;sy=(y>>15)&1
                        if xorsign and (not rir or family=='hmnmx2_xorsign_rir'):x&=0x7fff
                        if xorsign and not rir:y&=0x7fff
                        if ftz:
                            if x&0x7c00==0:x&=0x8000
                            if y&0x7c00==0:y&=0x8000
                        expmask,fracmask=(0x7e00,0x01ff) if e6m9 else ((0x7f80,0x007f) if bf16 else (0x7c00,0x03ff))
                        xn=(x&expmask)==expmask and (x&fracmask)!=0
                        yn=(y&expmask)==expmask and (y&fracmask)!=0
                        if nan_mode and (xn or yn):r=0x7fff
                        elif xn and not yn:r=y
                        elif yn and not xn:r=x
                        elif xn and yn:r=0x7fff
                        else:
                            if e6m9:
                                sx_cmp=(x&0x8000)!=0;sy_cmp=(y&0x8000)!=0
                                xm=x&0x7fff;ym=y&0x7fff
                                less=(sx_cmp if sx_cmp!=sy_cmp else (xm>ym if sx_cmp else xm<ym))
                                r=x if less==is_min else y
                            elif bf16:
                                xf=struct.unpack('<f',(x<<16).to_bytes(4,'little'))[0]
                                yf=struct.unpack('<f',(y<<16).to_bytes(4,'little'))[0]
                            else:
                                xf=struct.unpack('<e',x.to_bytes(2,'little'))[0]
                                yf=struct.unpack('<e',y.to_bytes(2,'little'))[0]
                            if not e6m9:
                                if xf==yf==0:r=(x|y) if is_min else (x&y)
                                else:r=x if (xf<yf)==is_min else y
                        if xorsign and not ((xn and yn) or (nan_mode and (xn or yn))):
                            r=(r&0x7fff)|((sx^sy)<<15)
                        if ftz and r&0x7c00==0:r&=0x8000
                        result|=r<<shift
                    return result
                expected=tuple(half_minmax(x,y) for x,y,_ in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,
                        tuple(hex(v) for v in vectors[mismatch]),hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith('vabsdiff4_'):
                signed='_s8_' in family;acc=family.endswith('_acc')
                expected=[]
                for x,y,c in vectors:
                    ds=[]
                    for lane in range(4):
                        a8=(x>>(lane*8))&255;b8=(y>>(lane*8))&255
                        if signed:
                            if a8&128:a8-=256
                            if b8&128:b8-=256
                        ds.append(abs(a8-b8))
                    expected.append(((sum(ds)+c)&0xffffffff) if acc else
                        sum((v&255)<<(lane*8) for lane,v in enumerate(ds)))
                if gpu!=tuple(expected):
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith('idp2a_'):
                _,mode,types=family.split('_');afmt,bfmt=types
                expected=[]
                for a,b,c in vectors:
                    total=c
                    for lane in range(2):
                        x=(a>>(16*lane))&0xffff
                        y=(b>>(8*(lane+(2 if mode=='hi' else 0))))&0xff
                        if afmt=='s' and x&0x8000:x-=0x10000
                        if bfmt=='s' and y&0x80:y-=0x100
                        total+=x*y
                    expected.append(total&0xffffffff)
                if gpu!=tuple(expected):
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith('i2ip_'):
                match=re.fullmatch(r'i2ip_(s2|u2|s4|u4|s8|u8|s16|u16)(?:_(nosat|satrelu))?',family)
                if not match:raise ValueError(f'bad I2IP fixture name {family}')
                fmt,mode=match.groups();fmt=fmt.upper();width=int(fmt[1:]);signed=fmt.startswith('S')
                lo=-(1<<(width-1)) if signed else 0
                hi=(1<<(width-1))-1 if signed else (1<<width)-1
                def cvt(value):
                    value=value if value<0x80000000 else value-0x100000000
                    if mode in ('satrelu',):value=max(0,min(hi,value))
                    elif mode=='nosat':pass
                    else:value=max(lo,min(hi,value))
                    return value&((1<<width)-1)
                expected=[]
                for x,y,c in vectors:
                    packed=(cvt(x)<<width)|cvt(y)
                    if width<16:packed|=(c&((1<<(32-2*width))-1))<<(2*width)
                    expected.append(packed&0xffffffff)
                if gpu!=tuple(expected):
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family=='f2ip':
                def cvt8(bits):
                    value=struct.unpack('<f',bits.to_bytes(4,'little'))[0]
                    if math.isnan(value):return 0
                    if value==float('inf'):rounded=127
                    elif value==float('-inf'):rounded=-128
                    else:rounded=round(value)
                    return max(-128,min(127,rounded))&0xff
                expected=tuple(((c&0xffff)<<16)|(cvt8(x)<<8)|cvt8(y)
                               for x,y,c in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,vectors[mismatch],hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith(('f2fp_f16_pack_','f2fp_bf16_pack_')):
                rn=not family.endswith('_rz');is_bf16=family.startswith('f2fp_bf16')
                relu='_relu' in family;satfinite='satfinite' in family
                def cvt16(bits):
                    sign=(bits>>16)&0x8000;exp=(bits>>23)&255;frac=bits&0x7fffff
                    if is_bf16:
                        if exp==255 and frac:return 0x7fff
                        hi=bits>>16;lo=bits&0xffff
                        if rn and (lo>0x8000 or (lo==0x8000 and hi&1)):hi+=1
                        out=hi&0xffff
                    elif exp==255:out=sign|(0x7c00 if frac==0 else 0x7e00)
                    elif exp==0:out=sign
                    else:
                        e=exp-127;sig=frac|0x800000
                        if e>15:out=sign|(0x7c00 if rn else 0x7bff)
                        elif e < -25:out=sign
                        else:
                            shift=13 if e>=-14 else -e-1
                            q=sig>>shift;rem=sig&((1<<shift)-1);half=1<<(shift-1)
                            if rn and (rem>half or (rem==half and q&1)):q+=1
                            if e>=-14:
                                if q>=0x800:e+=1;q=0x400
                                if e>15:out=sign|0x7c00
                                else:out=sign|((e+15)<<10)|(q&0x3ff)
                            else:out=sign|q
                    if satfinite:
                        if is_bf16 and (out&0x7fff)==0x7f80:out=(out&0x8000)|0x7f7f
                        if not is_bf16 and (out&0x7fff)==0x7c00:out=(out&0x8000)|0x7bff
                    if relu:
                        expmask,fracmask=(0x7f80,0x007f) if is_bf16 else (0x7c00,0x03ff)
                        if out&0x8000 and not ((out&expmask)==expmask and out&fracmask):out=0
                    return out
                if family in ('f2fp_f16_pack_imm','f2fp_f16_pack_c'):
                    constant=0x3fc00000
                    expected=tuple((cvt16(x)<<16)|cvt16(constant) for x,_,_ in vectors)
                else:expected=tuple((cvt16(x)<<16)|cvt16(y) for x,y,_ in vectors)
                def same_packed(got,want):
                    for shift in (0,16):
                        g=(got>>shift)&0xffff;e=(want>>shift)&0xffff
                        if is_bf16:
                            if g!=e:return False
                        elif g!=e and not ((g&0x7c00)==0x7c00 and (g&0x3ff)!=0 and
                                           (e&0x7c00)==0x7c00 and (e&0x3ff)!=0):
                            return False
                    return True
                if not all(same_packed(got,want) for got,want in zip(gpu,expected)):
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected))
                                  if not same_packed(got,want))
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,
                        tuple(hex(x) for x in vectors[mismatch][:2]),hex(gpu[mismatch]),hex(expected[mismatch])))
            if family in ('f2fp_e4m3_pack','f2fp_e5m2_pack'):
                fmt='E4M3' if family=='f2fp_e4m3_pack' else 'E5M2'
                expected=tuple((((c>>16)&0xffff)<<16)|
                    (fp8_reference(x,fmt)<<8)|fp8_reference(y,fmt)
                    for x,y,c in vectors)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,
                        tuple(hex(x) for x in vectors[mismatch]),hex(gpu[mismatch]),hex(expected[mismatch])))
            if family.startswith(('f2fp_e4m3_unpack_','f2fp_e5m2_unpack_')):
                fmt='E4M3' if family.startswith('f2fp_e4m3') else 'E5M2'
                expected=[]
                for word,_,_ in vectors:
                    lo=(fp8_to_f16_reference(word&0xff,fmt)|
                        (fp8_to_f16_reference((word>>8)&0xff,fmt)<<16))
                    hi=(fp8_to_f16_reference((word>>16)&0xff,fmt)|
                        (fp8_to_f16_reference((word>>24)&0xff,fmt)<<16))
                    if family.endswith('_relu'):
                        def relu_lanes(x):
                            lo16=x&0xffff;hi16=(x>>16)&0xffff
                            return (0 if lo16&0x8000 else lo16)|((0 if hi16&0x8000 else hi16)<<16)
                        lo=relu_lanes(lo);hi=relu_lanes(hi)
                    expected.append(lo|(hi<<32))
                expected=tuple(expected)
                if gpu!=expected:
                    mismatch=next(i for i,(got,want) in enumerate(zip(gpu,expected)) if got!=want)
                    raise AssertionError((family,'GPU/reference mismatch',mismatch,
                        tuple(hex(x) for x in vectors[mismatch]),hex(gpu[mismatch]),hex(expected[mismatch])))
            for index,(g,e) in enumerate(zip(gpu,actual)):
                if family.startswith('f2fp_f16_pack_'):
                    if not same_packed(e,g):raise AssertionError((family,index,
                        tuple(hex(x) for x in vectors[index][:2]),hex(e),hex(g)))
                    continue
                if family in HALF_FLOAT_OUTPUTS:
                    equal=all(((e>>shift)&0xffff)==((g>>shift)&0xffff) or
                        (((e>>shift)&0x7c00)==0x7c00 and ((e>>shift)&0x3ff)!=0 and
                         ((g>>shift)&0x7c00)==0x7c00 and ((g>>shift)&0x3ff)!=0) for shift in (0,16))
                    if not equal:raise AssertionError((family,index,vectors[index],hex(e),hex(g)))
                    continue
                if e!=g and not (family in FLOAT_OUTPUTS and nan(e,64 if family in DOUBLE_FAMILIES else 32) and nan(g,64 if family in DOUBLE_FAMILIES else 32)):
                    if known_min_subnormal_bug(family,vectors[index],e,g):
                        excluded.append(dict(index=index,inputs=[hex(v) for v in vectors[index]],
                            emulator=hex(e),gpu=hex(g),reason='Ghidra 12.1.4 RN minimum-subnormal underflow'))
                        continue
                    raise AssertionError((family,index,vectors[index],hex(e),hex(g)))
            passed=len(gpu)-len(excluded)
            suffix=f'; excluded {len(excluded)} known Ghidra underflow case(s)' if excluded else ''
            print(f'{family}: {passed} whole-kernel GPU/p-code comparisons passed{suffix}',flush=True)
            results.append(dict(family=family,passed=passed,excluded=excluded,steps=result['steps'],visited=result['visited'],
                                executed_mnemonics=sorted(executed)))
        (out/'results.json').write_text(json.dumps(dict(mode='whole-kernel',architecture='SM89',
            seed=1,threads_per_kernel=128,patched_fixtures=patched,results=results,exclusions=['warp communication','cross-thread shared memory',
            'MUFU approximation'],known_emulator_bug='Ghidra 12.1.4 RN results strictly between half and one minimum subnormal; exact inputs/results are recorded per fixture',
            floating_tolerance='bit-exact FSEL/FMNMX; other floating arithmetic except NaN payload/sign'),indent=2)+'\n')
    finally:driver.close()

if __name__=='__main__':main()
