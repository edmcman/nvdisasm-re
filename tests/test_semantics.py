"""Execute actual generated p-code against independent scalar references.

Usage: SASS_SLEIGH_OUT=... python3 tests/test_semantics.py SM89 [exact|readable]
"""
import json,math,os,random,struct,subprocess,sys
import itertools
from itertools import product
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from semantic_cases import encode
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT
from sass import ir,softfloat


def cases(sm,fp='exact'):
    arch=ir.load(sm)
    classes={k.name:k for k in arch.classes if not k.alternate}
    imad_records=json.loads((ROOT/'out'/sm/'semantics/IMAD.json').read_text())
    imad_hi_constant=next((x for x in imad_records if x['cls']=='imad_hi__RRC_RRC'),
                          next(x for x in imad_records if x['cls'].startswith('imad_hi__')))
    imad_hi_c_width=imad_hi_constant['sizes']['ISRC_C_SIZE']
    available=set(classes);out=[]
    def add(cls,fields,registers,expected,**extras):
        if cls not in available:return
        if sm=='SM120' and cls=='imnmx__RRR_RRR':
            # This architecture adds a second predicate input and two predicate
            # results. Its convention is intentionally an explicit fallback.
            expected=dict(error='sass_opaque_IMNMX')
        request=dict(word=encode(sm,cls,**fields),registers=registers,observe=[n for n in expected if not n.startswith("__")],**extras)
        if '__max_ops' in expected or '__native' in expected:request['inspect_pcode']=True
        out.append((cls,request,expected))
    def cx_source(klass,name,value,ur=4,offset=0,base=0x2000):
        """Encode a CX header and map its loaded word in GPU virtual memory."""
        atom=klass.operand_types[name];start=klass.format.index(atom)+1;children=[]
        for child in klass.format[start:]:
            if child.kind=='lit' and child.name==',':break
            if child.kind=='operand':children.append(child)
        ur_name=next(child.name for child in children if child.type in
                     ('UniformRegister','NonZeroUniformRegister','ZeroUniformRegister'))
        off_name=next(child.name for child in children if child.type in ('UImm','SImm'))
        bits,shift=(51,6) if sm=='SM90' else (45,4)
        header=base//(1<<shift)+(1<<bits)
        return ({ur_name:ur,off_name:offset},
                {'UR'+str(ur):header&0xffffffff,'UR'+str(ur+1):header>>32},
                [dict(space='ram',address=base+offset,
                      hex=(value&0xffffffff).to_bytes(4,'little').hex())])
    from bmsk_semantics import cases as bmsk_cases
    out.extend(bmsk_cases(sm))
    # CX children URb/Sb_offset describe a bounded constant-buffer address;
    # the dot product must consume the loaded 32-bit word from Sb.
    idp_cx=next((k for k in arch.classes if not k.alternate and
                 k.mnemonic=='IDP' and k.name.startswith('idp_4a__') and
                 any(atom.type=='CX' for atom in k.operand_types.values())),None)
    if idp_cx is not None:
        bits,shift=(51,6) if sm=='SM90' else (45,4)
        base=0x1000;header=base//(1<<shift)+(1<<bits)
        loaded=0x01020304
        fields=dict(Rd=8,Ra=2,URb=4,Sb_offset=0,Rc=6,
                    mode='4A',SrcAFmt='U8',SrcBFmt='U8')
        add(idp_cx.name,fields,
            {'R2':0x04030201,'R6':10,'UR4':header&0xffffffff,'UR5':header>>32},
            {'R8':30,'__native':True},
            memory=[dict(space='ram',address=base,
                         hex=loaded.to_bytes(4,'little').hex())])
    # CuLifter comparison: scalar bit operations. Expected values use an
    # independent per-bit reference (BREV) and Python's bit_count (POPC).
    def reverse32(value):
        return sum(((value>>bit)&1)<<(31-bit) for bit in range(32))
    bit_vectors=[0,1,0x80000000,0xffffffff,0x01234567,0xaaaaaaaa,0x55555555]
    rng=random.Random('brev_popc')
    bit_vectors += [rng.getrandbits(32) for _ in range(20)]
    for op in ('BREV','POPC'):
        for klass in (k for k in arch.classes if k.mnemonic==op and not k.alternate):
            src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
            if src is None:continue
            if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
            atom=klass.operand_types[src]
            if atom.type!='C':
                for i,value in enumerate(bit_vectors):
                    fields={'Rd':2,src:(4 if atom.type in ('Register','UniformRegister') else value)}
                    registers=({f'{src[:1]}4':value} if atom.type=='Register' else
                               {'UR4':value} if atom.type=='UniformRegister' else {})
                    source=value
                    if i==1 and src+'@invert' in klass.operand_types:
                        fields[src+'@invert']=1;source=(~value)&0xffffffff
                    result=reverse32(source) if op=='BREV' else source.bit_count()
                    add(klass.name,fields,registers,{'R2':result,'__native':True})
            if atom.type=='C':
                for bank,offset,value in ((2,16,0x01234567),(3,24,0x80000001)):
                    source=value
                    fields={'Rd':2,'Sb_bank':bank,'Sb_addr':offset}
                    if 'Sb@invert' in klass.operand_types:
                        fields['Sb@invert']=1;source=(~value)&0xffffffff
                    result=reverse32(source) if op=='BREV' else source.bit_count()
                    add(klass.name,fields,{}, {'R2':result,'__native':True},
                        memory=[dict(space='cbank',address=(bank<<32)|offset,
                                     hex=value.to_bytes(4,'little').hex())])
    # CuLifter scalar bit operations. FLO's ordinary form returns the highest
    # set-bit index; .SH returns the normalization shift. Both encode zero as
    # -1. SGXT selects one signed byte using the low two index bits.
    flo_values=[0,1,2,0x80000000,0xffffffff,0x01000000,0x00ffffff]
    for klass in (k for k in arch.classes if k.mnemonic=='FLO' and not k.alternate):
        src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if src is None or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        atom=klass.operand_types[src]
        for mode in ('nosh','SH'):
            for value in flo_values:
                fields={'Rd':2,'Pu':'PT','fmt':'S32','sh':mode}
                registers={}
                memory=[]
                if atom.type=='C':
                    fields.update(Sb_bank=2,Sb_addr=16)
                    memory=[dict(space='cbank',address=(2<<32)|16,hex=value.to_bytes(4,'little').hex())]
                elif atom.type in ('Register','UniformRegister'):
                    fields[src]=4
                    registers[('R' if atom.type=='Register' else 'UR')+'4']=value
                else:fields[src]=value
                result=((value.bit_length()-1) if mode=='nosh' else (32-value.bit_length())) if value else -1
                add(klass.name,fields,registers,{'R2':result&0xffffffff,'__native':True},memory=memory)
    sgxt_values=[0,0x7f80ff01,0x80ff007f,0xdeadbeef]
    for klass in (k for k in arch.classes if k.mnemonic=='SGXT' and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if src is None or klass.operand_types[src].type=='CX':continue
        atom=klass.operand_types[src]
        for idx in range(4):
            for value in sgxt_values:
                fields={'Rd':2,'Ra':4,'cw':'C','fmt':'S32'}
                registers={'R4':idx};memory=[]
                if atom.type=='C':
                    fields.update(Sb_bank=2,Sb_addr=16)
                    memory=[dict(space='cbank',address=(2<<32)|16,hex=value.to_bytes(4,'little').hex())]
                elif atom.type in ('Register','UniformRegister'):
                    fields[src]=6
                    registers[('R' if atom.type=='Register' else 'UR')+'6']=value
                else:fields[src]=value
                byte=(value>>(idx*8))&255
                result=byte if byte<128 else byte-256
                add(klass.name,fields,registers,{'R2':result&0xffffffff,'__native':True},memory=memory)
    # CuLifter's IDP.4A: independently unpack each byte as a signed or
    # unsigned integer, then accumulate four products modulo 2^32.
    def idp_ref(a,b,c,asigned,bsigned):
        total=c
        for i in range(4):
            x=(a>>(8*i))&255;y=(b>>(8*i))&255
            if asigned and x&128:x-=256
            if bsigned and y&128:y-=256
            total+=x*y
        return total&0xffffffff
    idp_words=[0,0xffffffff,0x807f01ff,0x01020304,0x7f80ff00]
    rng=random.Random('idp4a')
    idp_words += [rng.getrandbits(32) for _ in range(8)]
    for klass in (k for k in arch.classes if k.mnemonic=='IDP' and k.name.startswith('idp_4a__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if srcb is None or klass.operand_types[srcb].type=='CX':continue
        for afmt,bfmt in (('S8','S8'),('U8','U8'),('S8','U8'),('U8','S8')):
            for j,a in enumerate(idp_words):
                bval=idp_words[(j+3)%len(idp_words)];cval=idp_words[(j+5)%len(idp_words)]
                rd=2 if j%3==0 else 8
                fields={'Rd':rd,'Ra':4,'Rc':6,'mode':'4A','SrcAFmt':afmt,'SrcBFmt':bfmt}
                regs={'R4':a,'R6':cval};memory=[]
                if klass.operand_types[srcb].type=='C':
                    fields.update(Sb_bank=2,Sb_addr=16)
                    memory=[dict(space='cbank',address=(2<<32)|16,hex=bval.to_bytes(4,'little').hex())]
                else:
                    fields[srcb]=10
                    regs[('UR' if srcb=='URb' else 'R')+'10']=bval
                if j==1 and 'Rc@negate' in klass.operand_types:
                    fields['Rc@negate']=1;cval=(-cval)&0xffffffff
                add(klass.name,fields,regs,{f'R{rd}':idp_ref(a,bval,cval,afmt=='S8',bfmt=='S8'),'__native':True},memory=memory)
    # NVIDIA PTX ISA dp2a: two signed/unsigned 16-bit A lanes multiply
    # the selected pair of signed/unsigned bytes from B and accumulate into C.
    def idp2a_ref(a,b,c,asigned,bsigned,high):
        total=c
        for i in range(2):
            x=(a>>(16*i))&0xffff;y=(b>>(8*(i+(2 if high else 0))))&0xff
            if asigned and x&0x8000:x-=0x10000
            if bsigned and y&0x80:y-=0x100
            total+=x*y
        return total&0xffffffff
    idp2_words=[0,0xffffffff,0x80017fff,0x7fff8001,0x01020304]
    rng=random.Random('idp2a')
    idp2_words += [rng.getrandbits(32) for _ in range(8)]
    for klass in (k for k in arch.classes if k.mnemonic=='IDP' and k.name.startswith('idp_2a__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if srcb is None or klass.operand_types[srcb].type=='CX':continue
        for afmt in ('S16','U16'):
            for bfmt in ('S8','U8'):
                for mode in ('2A.LO','2A.HI'):
                    for j,a in enumerate(idp2_words):
                        bval=idp2_words[(j+3)%len(idp2_words)];cval=idp2_words[(j+5)%len(idp2_words)]
                        rd=2 if j%3==0 else 8
                        fields={'Rd':rd,'Ra':4,'Rc':6,'mode':mode,'SrcAFmt':afmt,'SrcBFmt':bfmt}
                        regs={'R4':a,'R6':cval};memory=[]
                        if klass.operand_types[srcb].type=='C':
                            fields.update(Sb_bank=2,Sb_addr=16)
                            memory=[dict(space='cbank',address=(2<<32)|16,hex=bval.to_bytes(4,'little').hex())]
                        else:
                            fields[srcb]=10
                            regs[('UR' if srcb=='URb' else 'R')+'10']=bval
                        if j==1 and 'Rc@negate' in klass.operand_types:
                            fields['Rc@negate']=1;cval=(-cval)&0xffffffff
                        expected=idp2a_ref(a,bval,cval,afmt=='S16',bfmt=='S8',mode.endswith('HI'))
                        add(klass.name,fields,regs,{f'R{rd}':expected,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='IDP' and
                  k.name.startswith('idp_2a__') and not k.alternate and
                  any(atom.type=='CX' for atom in k.operand_types.values())):
        srcb=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        a,bval,cval=0x80017fff,0x807f01ff,0x12345678
        fields={'Rd':8,'Ra':2,'Rc':6,'mode':'2A.HI','SrcAFmt':'S16','SrcBFmt':'S8'}
        cx_fields,cx_regs,memory=cx_source(klass,srcb,bval)
        fields.update(cx_fields)
        expected=idp2a_ref(a,bval,cval,True,True,True)
        add(klass.name,fields,{'R2':a,'R6':cval,**cx_regs},
            {'R8':expected,'__native':True},memory=memory)
    # VABSDIFF4's hardware ACC is a scalar sum of the four byte differences
    # plus Rc; SIMD_MRG returns the four differences packed in byte lanes.
    def vabsdiff4_ref(a,b,c,signed,red):
        diffs=[]
        for lane in range(4):
            x=(a>>(8*lane))&255;y=(b>>(8*lane))&255
            if signed:
                if x&128:x-=256
                if y&128:y-=256
            diffs.append(abs(x-y))
        if red=='SIMD_MRG':return sum((value&255)<<(8*lane) for lane,value in enumerate(diffs))
        return (sum(diffs)+c)&0xffffffff
    vabs_words=[0,0xffffffff,0x807f01ff,0xff010080,0x7f80ff01,0x01020304]
    for klass in (k for k in arch.classes if k.mnemonic=='VABSDIFF4' and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        srcc=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if not srcb or not srcc:continue
        for fmt in ('S8','U8'):
            if fmt not in arch.enums[klass.operand_types['fmt'].type]:continue
            for red in ('SIMD_MRG','ACC'):
                if red not in arch.enums[klass.operand_types['red'].type]:continue
                for j,a in enumerate(vabs_words):
                    bval=vabs_words[(j+2)%len(vabs_words)];cval=vabs_words[(j+4)%len(vabs_words)]
                    rd=2 if j%2==0 else 8
                    fields={'Rd':rd,'Ra':4,'fmt':fmt,'red':red,'Pu':'PT'}
                    regs={'R4':a};memory=[]
                    btype=klass.operand_types[srcb].type
                    if btype=='C':
                        fields.update(Sb_bank=2,Sb_addr=16)
                        memory.append(dict(space='cbank',address=(2<<32)|16,hex=bval.to_bytes(4,'little').hex()))
                    elif btype=='UImm':fields[srcb]=bval
                    else:
                        fields[srcb]=10;regs[('UR' if btype=='UniformRegister' else 'R')+'10']=bval
                    ctype=klass.operand_types[srcc].type
                    if ctype=='C':
                        fields.update(Sc_bank=3,Sc_addr=24)
                        memory.append(dict(space='cbank',address=(3<<32)|24,hex=cval.to_bytes(4,'little').hex()))
                    elif ctype=='UImm':fields[srcc]=cval
                    else:
                        fields[srcc]=12;regs[('UR' if ctype=='UniformRegister' else 'R')+'12']=cval
                    add(klass.name,fields,regs,
                        {f'R{rd}':vabsdiff4_ref(a,bval,cval,fmt=='S8',red),'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='VABSDIFF4' and
                  not k.alternate and any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        a,bval,cval=0x807f01ff,0xff010080,0x13579bdf
        fields={'Rd':8,'Ra':2,'fmt':'S8','red':'ACC','Pu':'PT'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval)
        fields.update(cx_fields);regs={'R2':a,**cx_regs}
        other=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if other and other!=cx_name:
            typ=klass.operand_types[other].type
            if typ=='Register':fields[other]=6;regs['R6']=cval
            elif typ=='UniformRegister':fields[other]=6;regs['UR6']=cval
            else:continue
        add(klass.name,fields,regs,
            {'R8':vabsdiff4_ref(a,bval,cval,True,'ACC'),'__native':True},memory=memory)
    # CuLifter's VIMNMX is predicate-selected scalar or packed min/max.
    def vimnmx_ref(a,b,width,signed,is_min,relu=False):
        mask=(1<<width)-1;result=0
        for lane in range(32//width):
            shift=lane*width
            x=(a>>shift)&mask;y=(b>>shift)&mask
            if signed:
                if x&(1<<(width-1)):x-=1<<width
                if y&(1<<(width-1)):y-=1<<width
            value=(min if is_min else max)(x,y)
            if relu and signed:value=max(value,0)
            result|=(value&mask)<<shift
        return result
    vimnmx_words=[0,0xffffffff,0x80007fff,0x7fff8000,0x12345678,0xdeadbeef]
    rng=random.Random('vimnmx')
    vimnmx_words += [rng.getrandbits(32) for _ in range(8)]
    for klass in (k for k in arch.classes if k.mnemonic=='VIMNMX' and not k.alternate):
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        # CX composite operands are address descriptors, not direct URb/Sb
        # values. Their semantics need an explicit provider and are covered by
        # opaque fallback tests rather than this direct-value arithmetic test.
        if not srcb or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        fmts=[('S32',32,True),('U32',32,False),('S16x2',16,True),('U16x2',16,False),('S8x4',8,True),('U8x4',8,False)]
        for fmt,width,signed in fmts:
            if fmt not in arch.enums[klass.operand_types['fmt'].type]:continue
            for relu in (('norelu','RELU') if signed else ('norelu',)):
                for is_min in (True,False):
                    for j,a in enumerate(vimnmx_words):
                        bval=vimnmx_words[(j+3)%len(vimnmx_words)];rd=2 if j%2 else 8
                        fields={'Rd':rd,'Ra':4,'fmt':fmt,'relu':relu,'Pp':'PT' if is_min else 'P0'}
                        for pred_name,pred_value in (('Pu','PT'),('Pv','PT')):
                            if pred_name in klass.operand_types:
                                pred_enum=arch.enums[klass.operand_types[pred_name].type]
                                fields[pred_name]=pred_value if pred_value in pred_enum else next(iter(pred_enum))
                        regs={'R4':a};memory=[]
                        btype=klass.operand_types[srcb].type
                        if btype=='C':
                            fields.update(Sb_bank=0,Sb_addr=0)
                            memory=[dict(space='cbank',address=0,hex=bval.to_bytes(4,'little').hex())]
                        elif btype in ('SImm','UImm'):fields[srcb]=bval
                        else:
                            fields[srcb]=10;regs[('UR' if srcb=='URb' else 'R')+'10']=bval
                        if not is_min:regs['P0']=0
                        add(klass.name,fields,regs,
                            {f'R{rd}':vimnmx_ref(a,bval,width,signed,is_min,relu=='RELU'),'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='VIMNMX' and
                  not k.alternate and 'pred' not in k.name and
                  any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        a,bval=0xfffffffe,3
        fields={'Rd':8,'Ra':2,'fmt':'S32','relu':'norelu','Pp':'PT'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
        add(klass.name,fields,{'R2':a,**cx_regs},
            {'R8':vimnmx_ref(a,bval,32,True,True),'__native':True},memory=memory)
    # CuLifter's VIADDMNMX adds modulo each lane width before min/max.
    def viaddmnmx_ref(a,b,c,width,signed,is_min,relu=False):
        mask=(1<<width)-1;result=0
        for lane in range(32//width):
            shift=lane*width
            total=(((a>>shift)&mask)+((b>>shift)&mask))&mask
            bound=(c>>shift)&mask
            x=total-(1<<width) if signed and total&(1<<(width-1)) else total
            y=bound-(1<<width) if signed and bound&(1<<(width-1)) else bound
            value=(min if is_min else max)(x,y)
            if relu and signed:value=max(value,0)
            result|=(value&mask)<<shift
        return result
    for klass in (k for k in arch.classes if k.mnemonic=='VIADDMNMX' and not k.alternate):
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        srcc=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if not srcb or not srcc or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        for fmt,width,signed in (('S32',32,True),('U32',32,False),('S16x2',16,True),('U16x2',16,False)):
            if fmt not in arch.enums[klass.operand_types['fmt'].type]:continue
            for relu in (('norelu','RELU') if signed else ('norelu',)):
                for is_min in (True,False):
                    for j,a in enumerate(vimnmx_words):
                        bval=vimnmx_words[(j+1)%len(vimnmx_words)];cval=vimnmx_words[(j+4)%len(vimnmx_words)]
                        negate=bool(j&1 and srcb+'@negate' in klass.operand_types)
                        effective_b=(-bval)&0xffffffff if negate else bval
                        rd=2 if j%2 else 8
                        fields={'Rd':rd,'Ra':4,'fmt':fmt,'relu':relu,'Pp':'PT' if is_min else 'P0'}
                        regs={'R4':a};memory=[]
                        btype=klass.operand_types[srcb].type
                        if btype=='C':
                            fields.update(Sb_bank=0,Sb_addr=0)
                            memory.append(dict(space='cbank',address=0,hex=bval.to_bytes(4,'little').hex()))
                        elif btype in ('SImm','UImm'):fields[srcb]=bval
                        else:
                            fields[srcb]=10;regs[('UR' if srcb=='URb' else 'R')+'10']=bval
                        ctype=klass.operand_types[srcc].type
                        if ctype=='C':
                            fields.update(Sc_bank=0,Sc_addr=4)
                            memory.append(dict(space='cbank',address=4,hex=cval.to_bytes(4,'little').hex()))
                        elif ctype in ('SImm','UImm'):fields[srcc]=cval
                        else:
                            fields[srcc]=12;regs[('UR' if srcc=='URc' else 'R')+'12']=cval
                        if negate:fields[srcb+'@negate']=1
                        if not is_min:regs['P0']=0
                        add(klass.name,fields,regs,
                            {f'R{rd}':viaddmnmx_ref(a,effective_b,cval,width,signed,is_min,relu=='RELU'),'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='VIADDMNMX' and
                  not k.alternate and any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        a,bval,cval=0x7fffffff,2,0x80000000
        fields={'Rd':8,'Ra':2,'Rc':6,'fmt':'U32','relu':'norelu','Pp':'PT'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
        add(klass.name,fields,{'R2':a,'R6':cval,**cx_regs},
            {'R8':viaddmnmx_ref(a,bval,cval,32,False,True),'__native':True},memory=memory)
    # CuLifter's VIMNMX3 is a lane-wise min/max; PT selects min and !PT max.
    def vimnmx3_ref(a,b,c,width,signed,is_min,relu):
        mask=(1<<width)-1;result=0
        for lane in range(32//width):
            shift=lane*width
            vals=[(v>>shift)&mask for v in (a,b,c)]
            if signed:vals=[v-(1<<width) if v&(1<<(width-1)) else v for v in vals]
            value=(min if is_min else max)(vals)
            if relu and signed:value=max(value,0)
            result|=(value&mask)<<shift
        return result
    vim_words=[0,0xffffffff,0x80007fff,0x7fff8000,0x12345678,0xdeadbeef]
    rng=random.Random('vimnmx3')
    vim_words += [rng.getrandbits(32) for _ in range(6)]
    for klass in (k for k in arch.classes if k.mnemonic=='VIMNMX3' and not k.alternate):
        srcb=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        srcc=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if not srcb or not srcc or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        fmts=[('S32',32,True),('U32',32,False),('S16x2',16,True),('U16x2',16,False)]
        for fmt,width,signed in fmts:
            if fmt not in arch.enums[klass.operand_types['fmt'].type]:continue
            for relu in (('norelu','RELU') if signed else ('norelu',)):
                for pred_name,pred_not,is_min in (('PT',0,True),('P0',0,False),('PT',1,False)):
                    for j,a in enumerate(vim_words):
                        bval=vim_words[(j+2)%len(vim_words)];cval=vim_words[(j+4)%len(vim_words)]
                        rd=2 if j%3==0 else 8
                        fields={'Rd':rd,'Ra':2,'Rc':6,'fmt':fmt,'relu':relu,'Pp':pred_name,'Pp@not':pred_not}
                        regs={'R2':a,'R6':cval};memory=[]
                        btype=klass.operand_types[srcb].type
                        if btype=='C':
                            fields.update(Sb_bank=0,Sb_addr=0)
                            memory.append(dict(space='cbank',address=0,hex=bval.to_bytes(4,'little').hex()))
                        elif btype in ('SImm','UImm'):fields[srcb]=bval
                        else:
                            fields[srcb]=10;regs[('UR' if srcb=='URb' else 'R')+'10']=bval
                        ctype=klass.operand_types[srcc].type
                        if ctype=='C':
                            fields.update(Sc_bank=0,Sc_addr=4)
                            memory.append(dict(space='cbank',address=4,hex=cval.to_bytes(4,'little').hex()))
                        elif ctype in ('SImm','UImm'):fields[srcc]=cval
                        elif srcc!='Rc':
                            fields[srcc]=12;regs[('UR' if srcc=='URc' else 'R')+'12']=cval
                        if not is_min:regs['P0']=0
                        add(klass.name,fields,regs,
                            {f'R{rd}':vimnmx3_ref(a,bval,cval,width,signed,is_min,relu=='RELU'),'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='VIMNMX3' and
                  not k.alternate and any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        a,bval,cval=0xfffffffe,3,1
        fields={'Rd':8,'Ra':2,'Rc':6,'fmt':'S32','relu':'norelu','Pp':'PT'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
        add(klass.name,fields,{'R2':a,'R6':cval,**cx_regs},
            {'R8':vimnmx3_ref(a,bval,cval,32,True,True,False),'__native':True},memory=memory)
    rng=random.Random('int_alu')
    for i in range(20):
        a,b,c=[rng.getrandbits(32) for _ in range(3)];regs={'R2':a,'R4':b,'R6':c}
        rd=2 if i%2 else 8 # overlap source/destination
        common=dict(Rd=rd,Ra=2,Rb=4,Rc=6)
        add('imad__RRR_RRR',dict(common,fmt='U32'),regs,{f'R{rd}':(a*b+c)&0xffffffff})
        add('imad_hi__RRR_RRR',dict(common,fmt='U32'),regs,{f'R{rd}':((a*b+c)&0xffffffffffffffff)>>32})
        add('iadd3_noimm__RRR_RRR',common,regs,{f'R{rd}':(a+b+c)&0xffffffff})
        add('iabs__RRR_R',dict(Rd=rd,Rb=4),regs,{f'R{rd}':abs(b if b<0x80000000 else b-0x100000000)&0xffffffff})
        add('sel__RRR_RRR',dict(Rd=rd,Ra=2,Rb=4,Pp=0),dict(regs,P0=i%2),{f'R{rd}':a if i%2 else b})
        add('imnmx__RRR_RRR',dict(Rd=rd,Ra=2,Rb=4,Pp=0,fmt='U32'),dict(regs,P0=1),{f'R{rd}':min(a,b)})
        add('lop3_lut__RRR_RRR',dict(common,imm8=0x96,Pp='PT'),regs,{f'R{rd}':a^b^c})
        add('lea_lo_noimm__RRR_RRR',dict(Rd=rd,Ra=2,Rb=4,scaleU5=i%8),regs,{f'R{rd}':((a<<(i%8))+b)&0xffffffff})
    # Mixed register banks and uniform aliases retain original md operand names.
    rng=random.Random('uniform')
    for i in range(12):
        a,b,c=[rng.getrandbits(32) for _ in range(3)]
        regs=dict(R2=a,R4=b,R6=c,UR2=a,UR4=b,UR6=c)
        add('imad__RUR_RUR',dict(Rd=2,Ra=2,URb=4,Rc=6,fmt='U32'),regs,dict(R2=(a*b+c)&0xffffffff))
        add('imad__RRU_RRU',dict(Rd=2,Ra=2,Rb=4,URc=6,fmt='U32'),regs,dict(R2=(a*b+c)&0xffffffff))
        add('iadd3_noimm__RUR_RUR',dict(Rd=2,Ra=2,URb=4,Rc=6),regs,dict(R2=(a+b+c)&0xffffffff))
        add('lea_lo_noimm__RUR_RUR',dict(Rd=2,Ra=2,URb=4,scaleU5=3),regs,dict(R2=((a<<3)+b)&0xffffffff))
        add('isetp__RUR_RUR_noEX',dict(Pu=0,Pv=1,Ra=2,URb=4,Pp='PT',icmp='LT',bop='AND',fmt='U32'),regs,dict(P0=int(a<b),P1=int(a>=b)))
        common=dict(URd=2,URa=2,URb=4,URc=6)
        add('uiadd3__URURUR_URURUR',common,regs,dict(UR2=(a+b+c)&0xffffffff,__max_ops=2))
        add('uimad__URURUR_URURUR',dict(common,fmt='U32'),regs,dict(UR2=(a*b+c)&0xffffffff,__max_ops=2))
        add('ulop3_lut__URURUR_URURUR',dict(common,imm8=0x96,UPp='UPT'),regs,dict(UR2=a^b^c))
        add('ulea_lo_noimm__URURUR_URURUR',dict(URd=2,URa=2,URb=4,scaleU5=3),regs,dict(UR2=((a<<3)+b)&0xffffffff))
        add('ushf__URURUR_URURUR',dict(common,fmt='U32',dir='R',cw='W',hilo='LO'),regs,dict(UR2=(((c<<32)|a)>>(b&31))&0xffffffff))
        add('uisetp__URURUR_URURUR',dict(UPu=0,UPv=1,URa=2,URb=4,UPp=0,icmp='LT',bop='AND',fmt='U32'),dict(regs,UP0=1),dict(UP0=int(a<b),UP1=int(a>=b)))
        add('usel__URURUR_UUU',dict(URd=2,URa=2,URb=4,UPp=0),dict(regs,UP0=i&1),dict(UR2=a if i&1 else b))
        add('umov__UR',dict(URd=2,URb=4),regs,dict(UR2=b,__max_ops=1))
        add('umov__UI',dict(URd=2,Sb=b),{},dict(UR2=b,__max_ops=1))
        if 'viadd__RRR_RRR' in classes:
            fmt=arch.enums[classes['viadd__RRR_RRR'].operand_types['fmt'].type]
            add('viadd__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,fmt='U32' if 'U32' in fmt else '32'),regs,dict(R2=(a+b)&0xffffffff))
        add('iadd_noimm__RRR_RRR',dict(Rd=2,Ra=2,Rb=4),regs,dict(R2=(a+b)&0xffffffff))
    add('uiadd3_x__URURUR_URURUR',dict(URd=8,URa=2,URb=4,URc=6,UPp='UPT',UPq='UPT'),{},dict(UR8=2,__native=True))
    add('uimad_x__URURUR_URURUR',dict(URd=8,URa=2,URb=4,URc=6,UPp='UPT'),{},dict(UR8=1,__native=True))
    add('ulea_lo_noimm_x__URURUR_URURUR',dict(URd=8,URa=2,URb=4,scaleU5=3,UPp='UPT'),{},dict(UR8=1,__native=True))
    # URZ is 63 in SM75-SM90 but 255 in SM100+, where 63 is the ordinary UR63
    # (ptxas emits UIMAD.WIDE.U32 ..., URZ with the eight-bit encoding).
    a,b,c=0x9abcdef1,0x87654321,0x13579bdf
    urz=dict(UR4=a,UR5=b)
    add('uimad_wide__URURUR_URURUR',dict(URd=2,URa=4,URb=5,URc='URZ',fmt='U32'),urz,dict(UR2=a*b&0xffffffff,UR3=a*b>>32,__native=True))
    add('uiadd3__URURUR_URURUR',dict(URd=2,URa=4,URb=5,URc='URZ'),urz,dict(UR2=(a+b)&0xffffffff,__native=True))
    if arch.enums['UniformRegister']['URZ']!=63:
        add('uiadd3__URURUR_URURUR',dict(URd=2,URa=4,URb=5,URc=63),dict(urz,UR63=c),dict(UR2=(a+b+c)&0xffffffff,__native=True))
    # Exhaust every byte selector, including sign replication and overlapping Rd.
    for mode in ('IDX','F4E','B4E','RC8','RC16','ECL','ECR'):
        for selector in range(16):
            a,c=0x80ff017f,0x55aa0081;data=a|(c<<32);want=0
            sel=selector*0x1111 if mode=='IDX' else selector
            for i in range(4):
                pick=(sel>>(4*i)&15) if mode=='IDX' else dict(F4E=(sel&3)+i,B4E=((sel&3)-i)&7,RC8=sel&3,RC16=((sel&1)*2)+(i&1),ECL=max(i,sel&3),ECR=min(i,sel&3))[mode]
                byte=data>>(8*(pick&7))&255
                if mode=='IDX' and pick&8:byte=255 if byte&128 else 0
                want|=byte<<(8*i)
            add('prmt__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6,pmode=mode),dict(R2=a,R4=sel,R6=c),dict(R2=want,__native=True))
    import struct,math
    edges=[0,0x80000000,1,0x80000001,0x007fffff,0x3f800000,0xbf800000,0x7f800000,0xff800000,0x7fc00000,0x7f800001,0xffc01234]
    for a in edges:
        for c in edges:
            for pred in (0,1):
                for ftz in (False,True):
                    def flush(v):return v&0x80000000 if ftz and v&0x7f800000==0 else v
                    x,y=flush(a),flush(c)
                    fields=dict(Rd=2,Ra=2,Rb=4,Pp=0,**({'ftz':'FTZ'} if ftz else {}))
                    regs=dict(R2=a,R4=c,P0=pred)
                    add('fsel__RRR_RRR',fields,regs,dict(R2=x if pred else y,**({'__native':True} if a==c==0 else {})))
                    xf,yf=[struct.unpack('<f',v.to_bytes(4,'little'))[0] for v in (x,y)]
                    if math.isnan(xf):want=0x7fffffff if math.isnan(yf) else y
                    elif math.isnan(yf):want=x
                    elif xf==yf==0:want=(x|y) if pred else (x&y)
                    else:want=x if (xf<yf)==bool(pred) else y
                    add('fmnmx__RRR_RRR',fields,regs,dict(R2=want,**({'__native':True} if a==c==0 else {})))
    # The three-input PTX min/max semantics use numeric-wins NaN selection,
    # canonical NaN under .NAN, and IEEE signed-zero ordering. Readable mode
    # intentionally remains the plain ordered-comparison formula.
    def fmnmx3_pick(x,y,is_min,exact):
        xf=struct.unpack('<f',x.to_bytes(4,'little'))[0]
        yf=struct.unpack('<f',y.to_bytes(4,'little'))[0]
        if exact:
            if math.isnan(xf):return y if not math.isnan(yf) else x
            if math.isnan(yf):return x
            if xf==yf==0:return (x|y) if is_min else (x&y)
        return y if (xf>yf if is_min else xf<yf) else x
    fmnmx3_values=[0x3f800000,0xbf800000,0x40000000,0x00000000,
                   0x80000000,0x7f800000,0xff800000,0x7fc00000,
                   0x7f812345,0xffc54321,0x00000001,0x80000001]
    for klass in (k for k in arch.classes if k.mnemonic=='FMNMX3' and not k.alternate):
        source_names=[next((n for n in group.split('/') if n in klass.operand_types),None)
                      for group in ('Ra/Sa','Rb/Sb/URb','Rc/Sc/URc')]
        if not all(source_names) or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        ftz_labels=[x for x in ('noftz','FTZ') if x in arch.enums[klass.operand_types['ftz'].type]]
        nan_labels=[x for x in ('nonan','NAN') if x in arch.enums[klass.operand_types['nan'].type]]
        for i,a in enumerate(fmnmx3_values):
            bval=fmnmx3_values[(i+3)%len(fmnmx3_values)]
            cval=fmnmx3_values[(i+5)%len(fmnmx3_values)]
            for ftz_mode,nan_mode,predicate,inverted in itertools.product(ftz_labels,nan_labels,(0,1),(0,1)):
                is_min=bool(predicate ^ inverted)
                fields={'Rd':8,'Pp':'P0','Pp@not':inverted,
                        'ftz':ftz_mode,'nan':nan_mode}
                regs={'P0':predicate}
                vals=(a,bval,cval)
                for name,value,reg in zip(source_names,vals,(2,4,6)):
                    typ=klass.operand_types[name].type
                    if typ=='Register':
                        fields[name]=reg;regs[f'R{reg}']=value
                    elif typ=='UniformRegister':
                        fields[name]=10;regs['UR10']=value
                    elif typ=='F32Imm':fields[name]=value
                    else:break
                else:
                    exact=fp=='exact'
                    prepared=vals
                    if exact and ftz_mode=='FTZ':
                        prepared=tuple((v&0x80000000) if (v&0x7f800000)==0 and (v&0x007fffff) else v for v in vals)
                    if exact and nan_mode=='NAN' and any(math.isnan(struct.unpack('<f',v.to_bytes(4,'little'))[0]) for v in prepared):
                        want=0x7fffffff
                    else:
                        want=fmnmx3_pick(fmnmx3_pick(prepared[0],prepared[1],is_min,exact),prepared[2],is_min,exact)
                    add(klass.name,fields,regs,{'R8':want,'__native':True})
    add('fmnmx_pred__RRR_RRR',dict(Rd=8,Pu=0,Ra=2,Rb=4,Pp='PT'),{},dict(error='sass_opaque_FMNMX'))
    # FMNMX.NAN returns canonical +NaN if either source is NaN; otherwise it
    # preserves the ordinary min/max result and signed-zero rule.
    fmnmx_nan_values=[0x7fc12345,0x7f812345,0xffc54321,0xff812345,
                      0x3f800000,0xc0000000,0,0x80000000,0x7f800000,0xff800000]
    for klass in (k for k in arch.classes if k.mnemonic=='FMNMX' and
                  k.name.startswith('fmnmx__') and not k.alternate):
        if 'nan' not in klass.operand_types:continue
        source=next((n for n in ('Rb','Sb','URb') if n in klass.operand_types),None)
        if source is None:continue
        atom=klass.operand_types[source]
        if atom.type=='CX':continue
        for i,a in enumerate(fmnmx_nan_values):
            c=fmnmx_nan_values[(i*3+1)%len(fmnmx_nan_values)]
            for ftz_mode in ('noftz','FTZ'):
                aa=(a&0x80000000) if ftz_mode=='FTZ' and a&0x7f800000==0 else a
                cc=(c&0x80000000) if ftz_mode=='FTZ' and c&0x7f800000==0 else c
                for is_min in (False,True):
                    fields={'Rd':8,'Ra':2,'Pp':'PT','Pp@not':int(not is_min),
                            'nan':'NAN','ftz':ftz_mode}
                    if 'xorsign' in klass.operand_types:fields['xorsign']='noxorsign'
                    regs={'R2':a};memory=[]
                    if atom.type=='Register':fields[source]=4;regs['R4']=c
                    elif atom.type=='UniformRegister':fields[source]=4;regs['UR4']=c
                    elif atom.type=='F32Imm':fields[source]=c
                    elif atom.type=='C':
                        fields.update(Sb_bank=2,Sb_addr=0x100)
                        memory=[dict(space='cbank',address=(2<<32)|0x100,
                                     hex=c.to_bytes(4,'little').hex())]
                    else:continue
                    anan=aa&0x7f800000==0x7f800000 and aa&0x007fffff!=0
                    cnan=cc&0x7f800000==0x7f800000 and cc&0x007fffff!=0
                    if anan or cnan:want=0x7fffffff
                    else:
                        af=struct.unpack('<f',aa.to_bytes(4,'little'))[0]
                        cf=struct.unpack('<f',cc.to_bytes(4,'little'))[0]
                        if af==cf==0:want=(aa|cc) if is_min else (aa&cc)
                        else:want=aa if (af<cf)==is_min else cc
                    add(klass.name,fields,regs,{'R8':want,'__native':True},
                        **({'memory':memory} if memory else {}))
    # PTX min.xorsign.abs: select on absolute values, then replace the result
    # sign with the XOR of the transformed source signs unless the result is NaN.
    for klass in (k for k in arch.classes if k.mnemonic=='FMNMX' and
                  k.name=='fmnmx__RRR_RRR' and not k.alternate and
                  'xorsign' in k.operand_types):
        pairs=[(0x3f800000,0xc0000000),(0xbf800000,0xc0400000),
               (0,0x80000000),(1,0x3f800000),(0x7fc12345,0xbf800000),
               (0x7fc12345,0xffc54321)]
        for a,c in pairs:
            for ftz_mode,nan_mode,is_min in itertools.product(('noftz','FTZ'),('nonan','NAN'),(False,True)):
                aa=(a&0x80000000) if ftz_mode=='FTZ' and a&0x7f800000==0 else a
                cc=(c&0x80000000) if ftz_mode=='FTZ' and c&0x7f800000==0 else c
                anan=aa&0x7f800000==0x7f800000 and aa&0x007fffff!=0
                cnan=cc&0x7f800000==0x7f800000 and cc&0x007fffff!=0
                if nan_mode=='NAN' and (anan or cnan):want=0x7fffffff
                elif anan:want=0x7fffffff if cnan else (cc&0x7fffffff)|((aa^cc)&0x80000000)
                elif cnan:want=(aa&0x7fffffff)|((aa^cc)&0x80000000)
                else:
                    xa=aa&0x7fffffff;xc=cc&0x7fffffff
                    af=struct.unpack('<f',xa.to_bytes(4,'little'))[0]
                    cf=struct.unpack('<f',xc.to_bytes(4,'little'))[0]
                    chosen=xa if (af<cf)==is_min else xc
                    want=chosen|((aa^cc)&0x80000000)
                fields={'Rd':8,'Ra':2,'Rb':4,'Pp':'PT','Pp@not':int(not is_min),
                        'xorsign':'XORSIGN','ftz':ftz_mode,'nan':nan_mode}
                add(klass.name,fields,{'R2':a,'R4':c},{'R8':want,'__native':True})
    add('mov__RI',dict(Rd=3,Sb=0xdeadbeef,PixMaskU04=15),{},dict(R3=0xdeadbeef,__max_ops=1))
    add('mov__RR',dict(Rd=3,Rb=2,PixMaskU04=15),dict(R2=0xdeadbeef),dict(R3=0xdeadbeef,__max_ops=1))
    add('mov__RR',dict(Rd=2,Rb=2,PixMaskU04=15),dict(R2=0xdeadbeef),dict(R2=0xdeadbeef))
    for cls,fields,registers,result,limit in [
        ('iadd3_noimm__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6),dict(R2=1,R4=2,R6=3),6,2),
        ('imad__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32'),dict(R2=2,R4=3,R6=4),10,2),
        ('lop3_lut__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,imm8=0x96,Pp='PT'),dict(R2=1,R4=2,R6=4),7,3),
        ('shf__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32',dir='R',cw='W',hilo='LO'),dict(R2=3,R4=1,R6=2),1,8)]:
        add(cls,fields,registers,dict(R8=result,__max_ops=limit))
    add('mov__RI',dict(Rd=3,Sb=5,PixMaskU04=15,Pg=0),dict(P0=0,R3=9),dict(R3=9))
    add('mov__RI',dict(Rd='RZ',Sb=5,PixMaskU04=15),{},dict(RZ=0,PT=1))
    add('imad_wide__RRR_RRR',dict(Rd='RZ',Ra=2,Rb=4,Rc=6,fmt='U32'),dict(R2=3,R4=4,R6=5,R7=1),dict(RZ=0,PT=1))
    add('imad_wide__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc='RZ',fmt='U32'),dict(R2=3,R4=4),dict(R8=12,R9=0))
    add('imad_wide__RRR_RRR',dict(Rd=254,Ra=2,Rb=4,Rc=6,fmt='U32'),dict(R2=2,R4=3,R6=1,R7=1),dict(error='sass_opaque_IMAD'))
    add('imad_wide__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32'),dict(R2=0xffffffff,R4=2,R6=3,R7=1),dict(R8=1,R9=3))
    add('isetp__RRR_RRR_noEX',dict(Pu=0,Pv=1,Ra=2,Rb=4,Pp=2,icmp='LT',bop='AND',fmt='U32'),dict(R2=2,R4=3,P2=1),dict(P0=1,P1=0))
    add('shf__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32',dir='R',cw='W',hilo='LO'),dict(R2=0xabcdef12,R4=36,R6=0x12345678),dict(R8=0x8abcdef1))
    for cls,fields,regs,expected in [
      ('fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),dict(R2=0x3f800000,R4=0x40000000),dict(R8=0x40400000)),
      ('fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4),dict(R2=0x3fc00000,R4=0x40000000),dict(R8=0x40400000)),
      ('ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6),dict(R2=0x3fc00000,R4=0x40000000,R6=0xbf800000),dict(R8=0x40000000)),
      ('f2i__Rb_32b',dict(Rd=8,Rb=2,dstfmt='S32',rnd='TRUNC'),dict(R2=0x3fc00000),dict(R8=1)),
      ('i2f__Rb_32b',dict(Rd=8,Rb=2),dict(R2=7),dict(R8=0x40e00000)),
      ('prmt__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,pmode='IDX'),dict(R2=0x03020100,R4=0x6420,R6=0x07060504),dict(R8=0x06040200)),
      ('s2r_',dict(Rd=8,SRa=0),dict(SR_LANEID=123),dict(R8=123))]:
        add(cls,fields,regs,expected)
    for op,space in [('ldg','ram'),('lds','shared'),('ldl','localmem')]:
        for sz,data,want in [('S8','ff',0xffffffff),('U16','3412',0x1234),('32','78563412',0x12345678),('64','0100000002000000',1),('128','01000000020000000300000004000000',1)]:
            expected={'R8':want}
            if sz in ('64','128'):expected['R9']=2
            if sz=='128':expected.update(R10=3,R11=4)
            add(op+'__sImmOffset',dict(Rd=8,Ra=2,Ra_offset=-4,sz=sz),dict(R2=0x104,R3=0),expected,memory=[dict(space=space,address=0x100,hex=data)])
    for mode,selector,want in [('F4E',1,0x04030201),('B4E',1,0x06070001),('RC8',2,0x02020202),('RC16',1,0x03020302),('ECL',2,0x03020202),('ECR',2,0x02020100)]:
        add('prmt__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,pmode=mode),dict(R2=0x03020100,R4=selector,R6=0x07060504),dict(R8=want))
    add('imad__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='S32'),dict(R2=0xfffffffe,R4=3,R6=1),dict(R8=0xfffffffb))
    add('imad_hi__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='S32'),dict(R2=0xfffffffe,R4=3,R6=1,R7=0),dict(R8=0xffffffff))
    add('iadd3_noimm__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,**{'Ra@negate':1}),dict(R2=2,R4=3,R6=4),dict(R8=5))
    add('fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,**{'Ra@negate':1}),dict(R2=0x3f800000,R4=0x40000000),dict(R8=0x3f800000))
    add('fadd__RRI_RI',dict(Rd=8,Ra=2,Sc=0x40000000),dict(R2=0x3f800000),dict(R8=0x40400000))
    add('imad__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32',**{'Rc@negate':1}),dict(R2=2,R4=3,R6=4),dict(R8=2))
    add('imad_hi__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt='U32'),dict(R2=0xffffffff,R4=2,R6=3,R7=1),dict(R8=3))
    wide_hi=4 if sm not in ('SM75','SM80') else 2
    add('imad_wide__RRC_RRC',dict(Rd=8,Pu=0,Ra=2,Rb=4,Sc_bank=2,Sc_addr=16,fmt='U32'),
        dict(R2=0xffffffff,R4=2),dict(R8=0xfffffffd,R9=wide_hi,P0=0),
        memory=[dict(space='cbank',address=(2<<32)|16,hex='ffffffff02000000')])
    add('imad_hi__RRC_RRC',dict(Rd=8,Pu=0,Ra=2,Rb=4,Sc_bank=2,Sc_addr=16,fmt='U32'),
        dict(R2=0xffffffff,R4=2),
        dict(R8=4,P0=0) if imad_hi_c_width>32 else dict(R8=0,P0=1),
        memory=[dict(space='cbank',address=(2<<32)|16,hex='ffffffff02000000')])
    if imad_hi_c_width==32 and 'imad_hi_x__RRC_RRC' in available:
        # Narrow HI addends must include the encoded predicate carry input.
        # With a zero product, C=UINT32_MAX and Pp=0/1 distinguish both the
        # result bit and the carry-out predicate.
        for carry,result,predicate in ((0,0xffffffff,0),(1,0,1)):
            add('imad_hi_x__RRC_RRC',dict(Rd=8,Pu=0,Ra=2,Rb=4,
                Sc_bank=2,Sc_addr=16,fmt='U32',Pp=1),
                dict(R2=0,R4=0,P1=carry),dict(R8=result,P0=predicate,__native=True),
                memory=[dict(space='cbank',address=(2<<32)|16,hex='ffffffff')])
    add('mov__RC',dict(Rd=8,Sb_bank=2,Sb_addr=16,PixMaskU04=15),{},dict(R8=0x12345678),memory=[dict(space='cbank',address=(2<<32)|16,hex='78563412')])
    for label,data,want in [('U8','ff',255),('S8','ff',0xffffffff),('U16','ffff',65535),
                            ('S16','ffff',0xffffffff),('32','78563412',0x12345678),
                            ('64','0100000002000000',1)]:
        expected=dict(UR8=want)
        if label=='64':expected['UR9']=2
        add('uldc_const__RCR',dict(URd=8,Sa_bank=2,Sa_addr=16,sz=label),{},expected,
            memory=[dict(space='cbank',address=(2<<32)|16,hex=data)])
    add('i2fp__RRR',dict(Rd=8,Rb=2,srcfmt='S32',dstfmt='F32'),dict(R2=0xfffffffe),dict(R8=0xc0000000))
    # F2FP.TF32.F32.PACK_B retains 10 fraction bits. The independent oracle
    # rounds the discarded 13-bit fraction field directly, including ties-even.
    tf32_values=[0,0x80000000,0x3f801000,0x3f803000,0xbf801000,0xbf803000,
                 0x3f7ff000,0x3f7ff001,0x7f800000,0xff800000]
    def tf32_round(bits,rn):
        sign=bits&0x80000000;mag=bits&0x7fffffff
        if mag&0x7f800000==0x7f800000:
            return bits&0xffffe000
        base=mag&0xffffe000;discarded=mag&0x1fff
        if rn and (discarded>0x1000 or
                   (discarded==0x1000 and ((base>>13)&1))):
            base+=0x2000
        return sign|base
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_tf32__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if src is None:continue
        typ=klass.operand_types[src].type
        for rnd in ('RN','RZ'):
            for i,bits in enumerate(tf32_values):
                fields={'Rd':8,'dstfmt':'TF32','srcfmt':'F32',
                        'merge':'PACK_B','rndMode':rnd,'relu':'norelu'}
                regs={};memory=[]
                if typ=='Register':fields[src]=4;regs['R4']=bits
                elif typ=='UniformRegister':fields[src]=10;regs['UR10']=bits
                elif typ=='F32Imm':fields[src]=bits
                elif typ=='C':
                    fields.update(Sb_bank=2,Sb_addr=0x100)
                    memory=[dict(space='cbank',address=(2<<32)|0x100,
                                 hex=bits.to_bytes(4,'little').hex())]
                else:continue
                add(klass.name,fields,regs,{'R8':tf32_round(bits,rnd=='RN'),
                                             '__native':True},memory=memory)
                for relu in ('norelu','RELU'):
                    if relu=='norelu':continue
                    result=tf32_round(bits,rnd=='RN')
                    if result&0x7f800000==0x7f800000 and result&0x007fffff:
                        result=0x7fffffff
                    elif result&0x80000000:
                        result=0
                    add(klass.name,dict(fields,relu=relu),regs,
                        {'R8':result,'__native':True},memory=memory)
    # F2FP.PACK_AB F32 -> F16/BF16, for scalar and uniform register inputs.
    def f32_bits(value):return struct.unpack('<I',struct.pack('<f',value))[0]
    packed_values=[0,0x80000000,0x3f800000,0xbf800000,0x3f801000,
        0x3f803000,0x33000000,0x33800000,0x387fc000,0x38800000,
        0x477fe000,0x477ff000,0x7f800000,0xff800000,0x7fc00000,
        0x7f800001,1,0x007fffff,0x00800000,0x7f7fffff,0xff7fffff]
    packed_values += [f32_bits(v) for v in (
        2.0**-41,2.0**-40,2.0**-39,2.0**-30,-2.0**-40,
        2.0**31,2.0**32-2.0**22,2.0**32-2.0**21,2.0**32)]
    def f16_pack_ref(bits,rn):
        sign=(bits>>16)&0x8000;exp=(bits>>23)&255;frac=bits&0x7fffff
        if exp==255:return sign|(0x7c00 if frac==0 else 0x7e00)
        if exp==0:return sign
        e=exp-127;sig=frac|0x800000
        if e>15:return sign|(0x7c00 if rn else 0x7bff)
        shift=13 if e>=-14 else -e-1
        if e < -25:return sign
        q=sig>>shift;rem=sig&((1<<shift)-1)
        half=1<<(shift-1)
        if rn and (rem>half or (rem==half and q&1)):q+=1
        if e>=-14:
            if q>=0x800:e+=1;q=0x400
            if e>15:return sign|0x7c00
            return sign|((e+15)<<10)|(q&0x3ff)
        return sign|q
    def bf16_pack_ref(bits,rn):
        exp=bits&0x7f800000;frac=bits&0x7fffff
        if exp==0x7f800000 and frac:return 0x7fff
        hi=bits>>16;lo=bits&0xffff
        if rn and (lo>0x8000 or (lo==0x8000 and hi&1)):hi+=1
        return hi&0xffff
    def e6m9_pack_ref(bits,rn):
        from fractions import Fraction as F
        kind,sign,value=softfloat.unpack(bits,32);signbit=sign<<15
        if kind=='nan':return 0x7fff
        if kind=='inf':return signbit|0x7e00
        value=abs(value)
        if not value:return signbit
        e=value.numerator.bit_length()-value.denominator.bit_length()
        if value<F(2)**e:e-=1
        if e < -30:
            scaled=value*(F(2)**39);q=scaled.numerator//scaled.denominator
            rem=scaled-q
            if rn and (rem>F(1,2) or (rem==F(1,2) and q&1)):q+=1
            return signbit|(0x200 if q>=0x200 else q)
        if e>31:return signbit|(0x7e00 if rn else 0x7dff)
        scaled=value/(F(2)**(e-9));q=scaled.numerator//scaled.denominator
        rem=scaled-q
        if rn and (rem>F(1,2) or (rem==F(1,2) and q&1)):q+=1
        if q>=0x400:e+=1;q=0x200
        if e>31:return signbit|(0x7e00 if rn else 0x7dff)
        return signbit|((e+31)<<9)|(q&0x1ff)
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcs=[next((n for n in group.split('/') if n in klass.operand_types),None)
              for group in ('Ra/URa','Rb/URb')]
        if not all(srcs) or any(klass.operand_types[n].type not in
              ('Register','UniformRegister') for n in srcs):continue
        if not all(n in klass.operand_types for n in ('dstfmt','srcfmt','merge','rndMode','relu')):continue
        for fmt in ('F16','BF16','E6M9'):
            if fmt not in arch.enums[klass.operand_types['dstfmt'].type]:continue
            if 'F32' not in arch.enums[klass.operand_types['srcfmt'].type]:continue
            if 'PACK_AB' not in arch.enums[klass.operand_types['merge'].type]:continue
            satfield='satfinite' if 'satfinite' in klass.operand_types else None
            for rnd in ('RN','RZ'):
                if rnd not in arch.enums[klass.operand_types['rndMode'].type]:continue
                for i,av in enumerate(packed_values):
                    bv=packed_values[(i*7+3)%len(packed_values)]
                    fields={'Rd':8,'dstfmt':fmt,'srcfmt':'F32','merge':'PACK_AB',
                            'rndMode':rnd,'relu':'norelu'}
                    if satfield:fields[satfield]='nosatfinite'
                    regs={}
                    for n,value,reg in zip(srcs,(av,bv),(2,4)):
                        typ=klass.operand_types[n].type;fieldreg=reg if typ=='Register' else reg+6
                        fields[n]=fieldreg
                        regs[('R' if typ=='Register' else 'UR')+str(fieldreg)]=value
                    conv={'F16':f16_pack_ref,'BF16':bf16_pack_ref,
                          'E6M9':e6m9_pack_ref}[fmt]
                    want=(conv(av,rnd=='RN')<<16)|conv(bv,rnd=='RN')
                    add(klass.name,fields,regs,{'R8':want,'__native':True})
        if 'RELU' in arch.enums[klass.operand_types['relu'].type]:
            sat_modes=('nosatfinite','SATFINITE') if satfield else (None,)
            for fmt in ('F16','BF16','E6M9'):
                if fmt not in arch.enums[klass.operand_types['dstfmt'].type]:continue
                conv={'F16':f16_pack_ref,'BF16':bf16_pack_ref,
                      'E6M9':e6m9_pack_ref}[fmt]
                for relu_mode,sat_mode in itertools.product(('norelu','RELU'),sat_modes):
                    if relu_mode=='norelu' and sat_mode!='SATFINITE':continue
                    for rnd in ('RN','RZ'):
                        if rnd not in arch.enums[klass.operand_types['rndMode'].type]:continue
                        for i,av in enumerate(packed_values):
                            bv=packed_values[(i*7+3)%len(packed_values)]
                            fields={'Rd':8,'dstfmt':fmt,'srcfmt':'F32','merge':'PACK_AB',
                                    'rndMode':rnd,'relu':relu_mode}
                            if satfield:fields[satfield]=sat_mode
                            regs={}
                            for n,value,reg in zip(srcs,(av,bv),(2,4)):
                                typ=klass.operand_types[n].type;fieldreg=reg if typ=='Register' else reg+6
                                fields[n]=fieldreg
                                regs[('R' if typ=='Register' else 'UR')+str(fieldreg)]=value
                            def mod(bits):
                                out=conv(bits,rnd=='RN')
                                sign=out&0x8000;mag=out&0x7fff
                                is_nan=(((mag&0x7e00)==0x7e00 and (mag&0x1ff)!=0)
                                        if fmt=='E6M9' else
                                        ((mag&0x7f80)==0x7f80 and (mag&0x7f)!=0)
                                        if fmt=='BF16' else
                                        ((mag&0x7c00)==0x7c00 and (mag&0x3ff)!=0))
                                if sat_mode=='SATFINITE':
                                    if fmt=='BF16' and mag==0x7f80:out=sign|0x7f7f
                                    if fmt=='F16' and mag==0x7c00:out=sign|0x7bff
                                    if fmt=='E6M9' and mag==0x7e00:out=sign|0x7dff
                                if relu_mode=='RELU':
                                    if is_nan:out=0x7fff if fmt in ('BF16','E6M9') else 0x7e00
                                    elif sign:out=0
                                return out
                            want=(mod(av)<<16)|mod(bv)
                            add(klass.name,fields,regs,{'R8':want,'__native':True})
    # E6M9 uses the same packed source forms as this class family. Exercise
    # immediate and direct constant-bank B operands as well as R/UR inputs.
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcs=[next((n for n in group.split('/')+['U'+n for n in group.split('/')]
                    if n in klass.operand_types),None) for group in ('Ra/URa','Rb/URb/Sb')]
        if not all(srcs) or not all(n in klass.operand_types for n in
                ('dstfmt','srcfmt','merge','rndMode','relu')):continue
        if 'E6M9' not in arch.enums[klass.operand_types['dstfmt'].type]:continue
        sat_atom=klass.operand_types.get('satfinite')
        if sat_atom and 'SATFINITE' in arch.enums.get(sat_atom.type,{}):
            sat_modes=('nosatfinite','SATFINITE')
        else:sat_modes=(None,)
        for rnd,relu_mode,sat_mode in itertools.product(
                ('RN','RZ'),('norelu','RELU'),sat_modes):
            for i,av in enumerate(packed_values):
                bv=packed_values[(i*7+3)%len(packed_values)]
                fields={'Rd':8,'dstfmt':'E6M9','srcfmt':'F32','merge':'PACK_AB',
                    'rndMode':rnd,'relu':relu_mode}
                if sat_mode is not None:fields['satfinite']=sat_mode
                regs={};memory=[]
                for n,value,reg in zip(srcs,(av,bv),(2,4)):
                    typ=klass.operand_types[n].type
                    if typ=='Register':fields[n]=reg;regs[f'R{reg}']=value
                    elif typ=='UniformRegister':fields[n]=reg+6;regs[f'UR{reg+6}']=value
                    elif typ=='F32Imm':fields[n]=value
                    elif typ=='C':
                        fields.update(Sb_bank=2,Sb_addr=0x100)
                        memory.append(dict(space='cbank',address=(2<<32)|0x100,
                                           hex=value.to_bytes(4,'little').hex()))
                    else:break
                else:
                    def e6_mod(bits):
                        out=e6m9_pack_ref(bits,rnd=='RN');mag=out&0x7fff
                        is_nan=(mag&0x7e00)==0x7e00 and (mag&0x1ff)!=0
                        if sat_mode=='SATFINITE' and mag==0x7e00:out=(out&0x8000)|0x7dff
                        if relu_mode=='RELU' and not is_nan and out&0x8000:out=0
                        return out
                    want=(e6_mod(av)<<16)|e6_mod(bv)
                    add(klass.name,fields,regs,{'R8':want,'__native':True},memory=memory)
    # MERGE_C converts one F32 input into the low half and copies the selected
    # half of the base operand into the high half. Exercise every ordinary
    # source layout and selector supported by the shared conversion emitter.
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_merge_c__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        base=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if not src or not base:continue
        if not all(n in klass.operand_types for n in
                   ('dstfmt','srcfmt','merge','rndMode','relu','extract')):continue
        sat_atom=klass.operand_types.get('satfinite')
        sat_modes=('nosatfinite','SATFINITE') if sat_atom else (None,)
        values=[0x3fc00000,0xbfc00000,0x7fc12345,0x7f800000,0xff800000]
        for fmt,rnd,relu_mode,sat_mode,extract in itertools.product(
                ('F16','BF16','E6M9'),('RN','RZ'),('norelu','RELU'),
                sat_modes,('H0','H1')):
            if fmt not in arch.enums[klass.operand_types['dstfmt'].type]:continue
            if 'F32' not in arch.enums[klass.operand_types['srcfmt'].type]:continue
            if 'MERGE_C' not in arch.enums[klass.operand_types['merge'].type]:continue
            if rnd not in arch.enums[klass.operand_types['rndMode'].type]:continue
            if extract not in arch.enums[klass.operand_types['extract'].type]:continue
            for i,bits in enumerate(values):
                fields={'Rd':8,'dstfmt':fmt,'srcfmt':'F32','merge':'MERGE_C',
                        'rndMode':rnd,'relu':relu_mode,'extract':extract}
                if sat_mode is not None:fields['satfinite']=sat_mode
                registers={};memory=[]
                for name,value,reg in ((src,bits,2),(base,0xdeadbeef,4)):
                    typ=klass.operand_types[name].type
                    if typ=='Register':fields[name]=reg;registers[f'R{reg}']=value
                    elif typ=='UniformRegister':fields[name]=reg;registers[f'UR{reg}']=value
                    elif typ=='F32Imm':fields[name]=value
                    elif typ=='C':
                        bank_name=name+'_bank';addr_name=name+'_addr'
                        fields[bank_name]=2;fields[addr_name]=0x100
                        memory.append(dict(space='cbank',address=(2<<32)|0x100,
                                           hex=value.to_bytes(4,'little').hex()))
                    else:break
                else:
                    conv={'F16':f16_pack_ref,'BF16':bf16_pack_ref,
                          'E6M9':e6m9_pack_ref}[fmt]
                    converted=conv(bits,rnd=='RN');sign=converted&0x8000
                    mag=converted&0x7fff
                    is_nan=(((mag&0x7e00)==0x7e00 and (mag&0x1ff)!=0)
                            if fmt=='E6M9' else
                            ((mag&0x7f80)==0x7f80 and (mag&0x7f)!=0)
                            if fmt=='BF16' else
                            ((mag&0x7c00)==0x7c00 and (mag&0x3ff)!=0))
                    if sat_mode=='SATFINITE':
                        if fmt=='BF16' and mag==0x7f80:converted=sign|0x7f7f
                        if fmt=='F16' and mag==0x7c00:converted=sign|0x7bff
                        if fmt=='E6M9' and mag==0x7e00:converted=sign|0x7dff
                    if relu_mode=='RELU':
                        if is_nan:converted=0x7fff if fmt in ('BF16','E6M9') else 0x7e00
                        elif sign:converted=0
                    basehalf=(0xdead if extract=='H1' else 0xbeef)
                    want=(basehalf<<16)|(converted&0xffff)
                    add(klass.name,fields,registers,{'R8':want,'__native':True},
                        memory=memory)
    # F2FP FP8 PACK_AB_MERGE_C, with exact rational-distance RN-even oracle.
    fp8_values=[0,0x80000000,0x00000001,0x007fffff,0x00800000,
        0x33800000,0x3f800000,0xbf800000,0x3f801000,0x3f803000,
        0x40600000,0x43e00000,0x43e10000,0x43e08000,0x477fe000,
        0x7f800000,0xff800000,0x7fc00000,0x7f800001]
    def fp8_decode(code,fmt):
        from fractions import Fraction as F
        mbits,bias=(3,7) if fmt=='E4M3' else (2,15)
        exp=(code>>mbits)&((1<<(4 if fmt=='E4M3' else 5))-1)
        mant=code&((1<<mbits)-1)
        if exp==0:return F(mant,1)*F(2)**(1-bias-mbits)
        return F((1<<mbits)+mant,1)*F(2)**(exp-bias-mbits)
    def fp8_ref(bits,fmt):
        kind,sign,value=softfloat.unpack(bits,32)
        s=sign<<7
        if kind=='nan':return s|0x7f
        if kind=='inf':return s|(0x7b if fmt=='E5M2' else 0x7e)
        value=abs(value)
        codes=[]
        for code in range(128):
            exp=code>>(2 if fmt=='E5M2' else 3)
            mant=code&((1<<(2 if fmt=='E5M2' else 3))-1)
            if fmt=='E5M2' and exp==31:continue
            if fmt=='E4M3' and exp==15 and mant==7:continue
            codes.append(code)
        maximum=codes[-1]
        if value>=fp8_decode(maximum,fmt):return s|maximum
        distances=[abs(value-fp8_decode(code,fmt)) for code in codes]
        best=min(distances);ties=[code for code,d in zip(codes,distances) if d==best]
        return s+(ties[0] if len(ties)==1 else next(code for code in ties if code&1==0))
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_f32_to_8b_downconvert__') and
                  not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        srcs=[next((n for n in group.split('/') if n in klass.operand_types),None)
              for group in ('Ra/URa','Rb/URb/Sb','Rc/URc/Sc')]
        if not all(srcs) or any(klass.operand_types[n].type=='CX' for n in srcs):continue
        if not all(n in klass.operand_types for n in
                   ('dstfmt','srcfmt','merge','rndMode','satfinite','relu','extract')):continue
        for fmt in ('E4M3','E5M2'):
            if fmt not in arch.enums[klass.operand_types['dstfmt'].type]:continue
            for extract in ('H0','H1'):
                if extract not in arch.enums[klass.operand_types['extract'].type]:continue
                for i,av in enumerate(fp8_values):
                    bv=fp8_values[(i*7+3)%len(fp8_values)]
                    cv=(0xa55a1234 ^ (i*0x01010101))&0xffffffff
                    fields={'Rd':6,'dstfmt':fmt,'srcfmt':'F32',
                        'merge':'PACK_AB_MERGE_C','rndMode':'RN',
                        'satfinite':'SATFINITE','relu':'norelu','extract':extract}
                    regs={};memory=[]
                    for n,value,reg in zip(srcs,(av,bv,cv),(2,4,6)):
                        typ=klass.operand_types[n].type
                        if typ=='Register':fields[n]=reg;regs[f'R{reg}']=value
                        elif typ=='UniformRegister':fields[n]=reg+6;regs[f'UR{reg+6}']=value
                        elif typ=='C':
                            fields.update(Sb_bank=2,Sb_addr=0x100)
                            memory.append(dict(space='cbank',address=(2<<32)|0x100,
                                               hex=value.to_bytes(4,'little').hex()))
                        elif typ=='F32Imm':fields[n]=value
                    cbits=(cv>>16)&0xffff if extract=='H1' else cv&0xffff
                    want=(cbits<<16)|(fp8_ref(av,fmt)<<8)|fp8_ref(bv,fmt)
                    add(klass.name,fields,regs,{'R6':want,'__native':True},memory=memory)
    # F2FP.F16.{E4M3,E5M2}.UNPACK_B widens both bytes in selected half.
    def fp8_to_f16(code,fmt):
        if fmt=='E3M4':
            sign=(code&0x80)<<8;exponent=(code>>4)&7;mantissa=code&15
            if exponent==7 and mantissa==15:return 0x7fff
            value=(mantissa/64 if exponent==0 else
                   (1+mantissa/16)*2**(exponent-3))
            if code&0x80:value=-value
            return sign|int.from_bytes(struct.pack('<e',value),'little')
        if fmt in ('E2M3','E3M2'):
            ebits,mbits,bias=(2,3,1) if fmt=='E2M3' else (3,2,3)
            sign=(code&0x20)<<10
            exponent=(code>>mbits)&((1<<ebits)-1)
            mantissa=code&((1<<mbits)-1)
            if exponent:
                return sign|((exponent-bias+15)<<10)|(mantissa<<(10-mbits))
            if not mantissa:return sign
            top=mantissa.bit_length()-1
            half_exp=15+1-bias-mbits+top
            fraction=(mantissa-(1<<top))<<(10-top)
            return sign|(half_exp<<10)|fraction
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
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_8b_upconvert__') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        source=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if source is None or klass.operand_types[source].type not in ('Register','UniformRegister'):continue
        if not all(n in klass.operand_types for n in
                   ('dstfmt','srcfmt','merge','rndMode','relu','extract_B')):continue
        typ=klass.operand_types[source].type
        reg=2 if typ=='Register' else 8
        for fmt in ('E4M3','E5M2','E2M3','E3M2','E3M4'):
            if fmt not in arch.enums[klass.operand_types['srcfmt'].type]:continue
            codes=range(64) if fmt in ('E2M3','E3M2') else range(256 if fmt=='E3M4' else 128)
            for extract in ('H0','H1'):
                if extract not in arch.enums[klass.operand_types['extract_B'].type]:continue
                fields={'Rd':8,'dstfmt':'F16','srcfmt':fmt,'merge':'UNPACK_B',
                    'rndMode':'RN','relu':'norelu','extract_B':extract,source:reg}
                for i in codes:
                    other=(63-i) if fmt in ('E2M3','E3M2') else ((255-i) if fmt=='E3M4' else (127-i))
                    pair=i|(other<<8)
                    word=pair if extract=='H0' else pair<<16
                    regs={};memory=[]
                    if typ=='Register':fields[source]=reg;regs[f'R{reg}']=word
                    elif typ=='UniformRegister':fields[source]=reg;regs[f'UR{reg}']=word
                    elif typ=='F32Imm':fields[source]=word
                    elif typ=='F32Imm':fields[source]=word
                    elif typ=='C':
                        fields.update(Sb_bank=2,Sb_addr=0x100)
                        memory=[dict(space='cbank',address=(2<<32)|0x100,
                                     hex=word.to_bytes(4,'little').hex())]
                    want=fp8_to_f16(i,fmt)|(fp8_to_f16(other,fmt)<<16)
                    add(klass.name,fields,regs,{'R8':want,'__native':True},memory=memory)
                for i in codes:
                    other=(63-i) if fmt in ('E2M3','E3M2') else ((255-i) if fmt=='E3M4' else (127-i))
                    pair=i|(other<<8)
                    word=pair if extract=='H0' else pair<<16
                    regs={};memory=[]
                    if typ=='Register':fields[source]=reg;regs[f'R{reg}']=word
                    elif typ=='UniformRegister':fields[source]=reg;regs[f'UR{reg}']=word
                    elif typ=='F32Imm':fields[source]=word
                    elif typ=='F32Imm':fields[source]=word
                    elif typ=='C':
                        fields.update(Sb_bank=2,Sb_addr=0x100)
                        memory=[dict(space='cbank',address=(2<<32)|0x100,
                                     hex=word.to_bytes(4,'little').hex())]
                    relu_fields=dict(fields,relu='RELU')
                    lo=fp8_to_f16(i,fmt);hi=fp8_to_f16(other,fmt)
                    want=(0 if lo&0x8000 else lo)|((0 if hi&0x8000 else hi)<<16)
                    add(klass.name,relu_fields,regs,{'R8':want,'__native':True},memory=memory)
    # SM100+ F2FP E2M1 byte upconvert: each selected byte contains two
    # nibbles, packed low nibble -> low F16 and high nibble -> high F16.
    def e2m1_to_f16(nibble):
        sign=(nibble&8)<<12;exponent=(nibble>>1)&3;mantissa=nibble&1
        if exponent:return sign|((exponent+14)<<10)|(mantissa<<9)
        return sign|(0x3800 if mantissa else 0)
    def e0m3_to_f16(nibble):
        sign=(nibble&8)<<12;magnitude=nibble&7
        return sign|int.from_bytes(struct.pack('<e',float(magnitude)),'little')
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_4b_upconvert__') and not k.alternate):
        source=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if source is None or not all(n in klass.operand_types for n in
                ('dstfmt','srcfmt','merge','rndMode','relu','selB')):continue
        typ=klass.operand_types[source].type
        for srcfmt in ('E2M1','E0M3'):
            if srcfmt not in arch.enums[klass.operand_types['srcfmt'].type]:continue
            convert=e2m1_to_f16 if srcfmt=='E2M1' else e0m3_to_f16
            for selector in ('B0','B1','B2','B3'):
                if selector not in arch.enums[klass.operand_types['selB'].type]:continue
                sel=int(selector[1:])
                for relu in ('norelu','RELU'):
                    for byte in range(256):
                        word=byte<<(sel*8)
                        fields={'Rd':8,'dstfmt':'F16','srcfmt':srcfmt,'merge':'UNPACK_B',
                            'rndMode':'RN','relu':relu,'selB':selector}
                        regs={}
                        if typ=='Register':fields[source]=2;regs['R2']=word
                        elif typ=='UniformRegister':fields[source]=4;regs['UR4']=word
                        elif typ=='F32Imm':fields[source]=word
                        lo=convert(byte&0xf);hi=convert(byte>>4)
                        if relu=='RELU':
                            lo=0 if lo&0x8000 else lo;hi=0 if hi&0x8000 else hi
                        add(klass.name,fields,regs,{'R8':lo|(hi<<16),'__native':True})
    # SM100+ scaled FP4 upconvert. PTX specifies per-lane UE8M0 multiplication
    # after unpacking; low/high nibbles pair with low/high scale bytes.
    def fp4_scaled_bf16(nibble,fmt,scale,relu):
        mag=nibble&7;sign=(nibble&8)!=0
        values=([0.0,0.5,1.0,1.5,2.0,3.0,4.0,6.0] if fmt=='E2M1' else
                [0.0,1.0,2.0,3.0,4.0,5.0,6.0,7.0])
        value=-values[mag] if sign else values[mag]
        if scale==0xff:return 0x7fff
        factor=2.0**(-127 if scale==0 else scale-127)
        scaled=value*factor
        try:bits=int.from_bytes(struct.pack('<f',scaled),'little')
        except OverflowError:bits=(0xff800000 if scaled<0 else 0x7f800000)
        if (bits&0x7f800000)==0x7f800000:
            if bits&0x007fffff:return 0x7fff
            out=(bits>>16)&0xffff
        else:out=(bits+0x7fff+((bits>>16)&1))>>16
        if (out&0x7fff)==0x7f80:out=(out&0x8000)|0x7f7f
        if relu and out&0x8000 and not ((out&0x7f80)==0x7f80 and (out&0x7f)!=0):return 0
        return out
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_4b_upconvert_scale__') and not k.alternate):
        bsrc=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        csrc=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if bsrc is None or csrc is None:continue
        btype=klass.operand_types[bsrc].type;ctype=klass.operand_types[csrc].type
        for fmt in ('E2M1','E0M3'):
            for selector in ('B0','B1','B2','B3'):
                if selector not in arch.enums[klass.operand_types['selB'].type]:continue
                for relu in ('norelu','RELU'):
                    for i in range(256):
                        byte=(i&15)|(((i>>4)&15)<<4)
                        bword=byte<<(int(selector[1:])*8)
                        scales=i|((255-i)<<8)
                        fields={'Rd':8,'dstfmt':'BF16','srcfmt':fmt,'merge':'UNPACK_B',
                            'rndMode':'RN','satfinite':'SATFINITE','relu':relu,'selB':selector}
                        regs={}
                        if btype=='Register':fields[bsrc]=2;regs['R2']=bword
                        elif btype=='UniformRegister':fields[bsrc]=4;regs['UR4']=bword
                        else:fields[bsrc]=bword
                        if ctype=='Register':fields[csrc]=6;regs['R6']=scales
                        elif ctype=='UniformRegister':fields[csrc]=8;regs['UR8']=scales
                        else:fields[csrc]=scales
                        lo=fp4_scaled_bf16(byte&15,fmt,scales&0xff,relu=='RELU')
                        hi=fp4_scaled_bf16(byte>>4,fmt,(scales>>8)&0xff,relu=='RELU')
                        add(klass.name,fields,regs,{'R8':lo|(hi<<16),'__native':True})
    # SM100+ F16/BF16 to E2M1x4: B supplies the low byte and C the next
    # byte, as established by the ptxas UNPACK_B_MERGE_C lowering.
    def half_to_e2m1(raw,fmt,relu):
        sign=(raw>>12)&8;mag=raw&0x7fff
        thresholds=((0x3400,0x3a00,0x3d00,0x3f00,0x4100,0x4300,0x4500,0x7c00)
                    if fmt=='F16' else
                    (0x3e80,0x3f40,0x3fa0,0x3fe0,0x4020,0x4060,0x40a0,0x7f80))
        if mag>thresholds[-1]:return 7 # NaN -> positive maxnorm
        code=7
        for idx,(threshold,op) in enumerate(zip(thresholds[:7],
                ('>','>=','>','>=','>','>=','>')),start=1):
            if mag>threshold if op=='>' else mag>=threshold:
                continue
            code=idx-1;break
        out=sign|code
        return 0 if relu and sign else out
    def half_to_e0m3(raw,fmt,relu):
        sign=(raw>>15)&1
        value=(struct.unpack('<e',raw.to_bytes(2,'little'))[0] if fmt=='F16' else
               struct.unpack('<f',(raw<<16).to_bytes(4,'little'))[0])
        # NaNs canonicalize to the positive max code before RELU, as for E2M1.
        if math.isnan(value):return 7
        if math.isinf(value):code=7
        else:code=min(7,round(abs(value)))
        if relu and sign:return 0
        return (sign<<3)|code
    fp4_edges={
        'F16':[0,1,0x03ff,0x0400,0x8000,0x8001,0x7c00,0xfc00,
               0x7c01,0x7e00,0xfc01,0xfe00],
        'BF16':[0,1,0x007f,0x0080,0x8000,0x8001,0x7f80,0xff80,
                0x7f81,0x7fc0,0xff81,0xffc0]}
    fp4_thresholds={
        'F16':(0x3400,0x3a00,0x3d00,0x3f00,0x4100,0x4300,0x4500),
        'BF16':(0x3e80,0x3f40,0x3fa0,0x3fe0,0x4020,0x4060,0x40a0)}
    e0m3_thresholds={
        'F16':(0x3800,0x3e00,0x4100,0x4300,0x4480,0x4580,0x4680),
        'BF16':(0x3f00,0x3fc0,0x4020,0x4060,0x4090,0x40b0,0x40d0)}
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_f16_to_4b_downconvert__') and not k.alternate):
        bsrc=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        csrc=next((n for n in ('Rc','URc','Sc') if n in klass.operand_types),None)
        if bsrc is None or csrc is None:continue
        btype=klass.operand_types[bsrc].type;ctype=klass.operand_types[csrc].type
        for fmt in ('F16','BF16'):
            vals=set(fp4_edges[fmt])
            for threshold in set(fp4_thresholds[fmt])|set(e0m3_thresholds[fmt]):
                vals.update((threshold-1,threshold,threshold+1,
                             threshold|0x8000,(threshold|0x8000)-1,
                             (threshold|0x8000)+1))
            rng=random.Random('f2fp_4b_down')
            vals.update(rng.getrandbits(16) for _ in range(24))
            vals=sorted(vals)
            # Pair each boundary with a different lane to catch lane swaps.
            for dstfmt in ('E2M1','E0M3'):
                convert=half_to_e2m1 if dstfmt=='E2M1' else half_to_e0m3
                for relu in ('norelu','RELU'):
                    for i,x in enumerate(vals):
                        y=vals[(i*7+3)%len(vals)]
                        bword=x|(y<<16);cword=vals[(i*11+5)%len(vals)]|(vals[(i*13+9)%len(vals)]<<16)
                        fields={'Rd':8,'dstfmt':dstfmt,'srcfmt':fmt,
                            'merge':'UNPACK_B_MERGE_C','rndMode':'RN',
                            'satfinite':'SATFINITE','relu':relu}
                        regs={}
                        if btype=='Register':fields[bsrc]=2;regs['R2']=bword
                        elif btype=='UniformRegister':fields[bsrc]=4;regs['UR4']=bword
                        else:fields[bsrc]=bword
                        if ctype=='Register':fields[csrc]=6;regs['R6']=cword
                        elif ctype=='UniformRegister':fields[csrc]=8;regs['UR8']=cword
                        else:fields[csrc]=cword
                        lanes=[convert((bword>>(16*j))&0xffff,fmt,relu=='RELU') for j in range(2)]
                        lanes += [convert((cword>>(16*j))&0xffff,fmt,relu=='RELU') for j in range(2)]
                        want=sum(lane<<(4*j) for j,lane in enumerate(lanes))
                        add(klass.name,fields,regs,{'R8':want,'__native':True})
    # SM100+ UE8M0 to BF16x2. CUDA's host reference maps 0xff to canonical
    # NaN, 0 to the 2^-127 BF16 subnormal, and other bytes by shifting left 7.
    def e8m0_to_bf16(byte):
        if byte==0xff:return 0x7fff
        if byte==0:return 0x0040
        return byte<<7
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name.startswith('f2fp_E8_upconvert__') and not k.alternate):
        source=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if source is None or not all(n in klass.operand_types for n in
                ('dstfmt','srcfmt','merge','rndMode','extract_B')):continue
        typ=klass.operand_types[source].type
        for extract in ('H0','H1'):
            for byte in range(256):
                other=255-byte
                pair=byte|(other<<8)
                word=pair if extract=='H0' else pair<<16
                fields={'Rd':8,'dstfmt':'BF16','srcfmt':'E8','merge':'UNPACK_B',
                    'rndMode':'RN','extract_B':extract}
                regs={}
                if typ=='Register':fields[source]=2;regs['R2']=word
                elif typ=='UniformRegister':fields[source]=4;regs['UR4']=word
                elif typ=='F32Imm':fields[source]=word
                lo=e8m0_to_bf16(byte);hi=e8m0_to_bf16(other)
                add(klass.name,fields,regs,{'R8':lo|(hi<<16),'__native':True})
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name=='f2fp__RCxR' and not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        a,bval=0x3f800000,0x40000000
        fields={'Rd':8,'Ra':2,'dstfmt':'F16','srcfmt':'F32','merge':'PACK_AB',
                'rndMode':'RN','relu':'norelu','satfinite':'nosatfinite'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
        add(klass.name,fields,{'R2':a,**cx_regs},
            {'R8':0x3c004000,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='F2FP' and
                  k.name in ('f2fp_merge_c__RCxR','f2fp_merge_c__RRCx') and
                  not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        fields={'Rd':8,'dstfmt':'F16','srcfmt':'F32','merge':'MERGE_C',
                'rndMode':'RN','relu':'norelu','satfinite':'nosatfinite','extract':'H1'}
        regs={}
        if cx_name=='Sb':fields['Rc']=6;regs['R6']=0xaabbccdd
        else:fields['Rb']=2;regs['R2']=0x3f800000
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,
            0x3f800000 if cx_name=='Sb' else 0xaabbccdd);fields.update(cx_fields);regs.update(cx_regs)
        add(klass.name,fields,regs,{'R8':0xaabb3c00,'__native':True},memory=memory)
    if 'f2i__Rb_32b' in classes:
        f2i_values=[0x7fc00000,0x7f800001,0x7f800000,0xff800000,
                          (0x4effffff,0x7fffff80),(0x4f000000,0x7fffffff),
                          (0xcf000000,0x80000000),(0xcf000001,0x80000000),
                          (0x3fc00000,1),(0xbfc00000,0xffffffff),(0x3f000000,0),
                          (0xbf000000,0),(0x00000001,0),(0x80000001,0)]
        f2i_values=[x if isinstance(x,int) else x[0] for x in f2i_values]
        def f2i_ref(bits):
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
        for bits in f2i_values:
            for absolute in (0,1):
                for negate in (0,1):
                    effective=bits
                    if absolute:effective &= 0x7fffffff
                    if negate:effective ^= 0x80000000
                    fields=dict(Rd=8,Rb=2,dstfmt='S32',rnd='TRUNC',ntz='NTZ',
                                **{'Rb@absolute':absolute,'Rb@negate':negate})
                    add('f2i__Rb_32b',fields,dict(R2=bits),
                        dict(R8=f2i_ref(effective),__native=True))
        if fp=='readable':
            for rnd in ('ROUND','FLOOR','CEIL'):
                add('f2i__Rb_32b',dict(Rd=8,Rb=2,dstfmt='S32',rnd=rnd,ntz='NTZ'),
                    dict(R2=0x3fe00000),dict(R8=1,__native=True))
    # F2IP rounds two F32 inputs to nearest-even, saturates each to S8/U8,
    # and copies C[15:0] into the high half of the packed result.
    f2ip_values=[0,0x80000000,0x3f000000,0xbf000000,0x3fc00000,0xbfc00000,
                 0x40200000,0xc0200000,0x42ff0000,0xc3000000,0x43000000,
                 0xc2ff0000,0x42ff8000,0xc2ff8000,0x437f8000,0x7f800000,
                 0xff800000,0x7fc00000,0x7f800001,0x4f000000,0xcf000000]
    def f2ip_byte(bits,signed):
        x=struct.unpack('<f',bits.to_bytes(4,'little'))[0]
        if math.isnan(x):return 0
        lo,hi=(-128,127) if signed else (0,255)
        if math.isinf(x):value=hi if x>0 else lo
        else:value=max(lo,min(hi,round(x)))
        return value&0xff
    for klass in (k for k in arch.classes if k.mnemonic=='F2IP' and not k.alternate):
        srcs=[next((n for n in group.split('/') if n in klass.operand_types),None)
              for group in ('Ra','Rb/URb/Sb','Rc/Sc/URc')]
        if not all(srcs) or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        for dstfmt,signed in (('S8',True),('U8',False)):
            if dstfmt not in arch.enums[klass.operand_types['dstfmt'].type]:continue
            for j in range(len(f2ip_values)):
                a=f2ip_values[j];bval=f2ip_values[(j+7)%len(f2ip_values)]
                cval=(0x1357+j*0x101)&0xffffffff;rd=2 if j%2==0 else 8
                fields={'Rd':rd,'dstfmt':dstfmt,'srcfmt':'F32','rnd':'ROUND',
                        'ntz':'NTZ','relu':'norelu'}
                regs={};memory=[]
                for name,value,reg in zip(srcs,(a,bval,cval),(2,4,6)):
                    typ=klass.operand_types[name].type
                    if typ=='Register':fields[name]=reg;regs[f'R{reg}']=value
                    elif typ=='UniformRegister':fields[name]=10;regs['UR10']=value
                    elif typ=='C':
                        offset={'Sb':0,'Sc':4}.get(name,8)
                        fields.update({name+'_bank':0,name+'_addr':offset})
                        memory.append(dict(space='cbank',address=offset,
                                           hex=value.to_bytes(4,'little').hex()))
                    elif typ in ('F32Imm','SImm','UImm'):fields[name]=value
                    else:break
                else:
                    want=((cval&0xffff)<<16)|(f2ip_byte(a,signed)<<8)|f2ip_byte(bval,signed)
                    add(klass.name,fields,regs,{f'R{rd}':want,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='F2IP' and
                  not k.alternate and any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        a,bval,cval=0x3fc00000,0x40200000,0x12345678
        fields={'Rd':8,'Ra':2,'Rc':6,'dstfmt':'S8','srcfmt':'F32',
                'rnd':'ROUND','ntz':'NTZ','relu':'norelu'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
        want=((cval&0xffff)<<16)|(f2ip_byte(a,True)<<8)|f2ip_byte(bval,True)
        add(klass.name,fields,{'R2':a,'R6':cval,**cx_regs},
            {'R8':want,'__native':True},memory=memory)
    if 'f2ip__RRR' in classes:
        add('f2ip__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,dstfmt='S8',srcfmt='F32',
            rnd='TRUNC',ntz='NTZ',relu='norelu'),dict(R2=0x3f800000,R4=0x40000000,
            R6=0),dict(error='sass_opaque_F2IP'))
    # I2IP inserts two saturated or truncated S32 values into a packed word.
    # For sub-byte formats the unused high bits come from C; S16/U16 fill the
    # full result and ptxas uses RZ in that source slot.
    i2ip_values=[0,1,-1,-3,3,-9,7,8,15,16,127,-128,255,-256,
                 32767,-32768,0x12345678,-0x1234567]
    for klass in (k for k in arch.classes if k.mnemonic=='I2IP' and not k.alternate):
        srcs=[next((n for n in group.split('/') if n in klass.operand_types),None)
              for group in ('Ra','Rb/URb/Sb','Rc')]
        if not all(srcs) or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        dst_enum=arch.enums[klass.operand_types['dstfmt'].type]
        extract_field=next(n for n,x in klass.operand_types.items()
                           if x.type in ('EXTRACT','HSEL','ONLY24','ONLY28'))
        ext_enum=arch.enums[klass.operand_types[extract_field].type]
        sat_field=next(n for n,x in klass.operand_types.items()
                       if x.type in ('SATONLY','SATRELU','SATRELU_RELU'))
        sat_enum=arch.enums[klass.operand_types[sat_field].type]
        labels=[label for label in ('S2','U2','S4','U4','S8','U8','S16','U16') if label in dst_enum]
        modes=[label for label in ('nosatrelu','nosatrelu_relu','SAT','SATRELU','RELU') if label in sat_enum]
        for fmt in labels:
            width=int(fmt[1:]);signed=fmt.startswith('S')
            extracts=(('24',) if width==4 else ('28',) if width==2 else ('H0','H1'))
            extracts=tuple(x for x in extracts if x in ext_enum)
            for extract in extracts:
                for mode in modes:
                    if not signed and mode in ('SATRELU','RELU'):continue
                    sat=mode in ('SAT','SATRELU');relu=mode in ('RELU','SATRELU')
                    lo=-(1<<(width-1)) if signed else 0
                    hi=(1<<(width-1))-1 if signed else (1<<width)-1
                    def i2ip_convert(v):
                        if sat:v=max(lo,min(hi,v))
                        if relu:v=max(0,v)
                        return v&((1<<width)-1)
                    # Exercise truncation, both saturation bounds, and RELU with
                    # values chosen around the target range for this element size.
                    samples=[0,1,-1,lo-1,lo,hi,hi+1,0x12345678]
                    for j,avalue in enumerate(samples):
                        bvalue=samples[(j+3)%len(samples)]
                        cvalue=(0xa53c91e7+j*0x10203)&0xffffffff
                        rd=2 if j==7 else 8
                        fields={'Rd':rd,'dstfmt':fmt,'srcfmt':'S32',sat_field:mode,
                                extract_field:extract}
                        regs={};memory=[]
                        for name,value,reg in zip(srcs,(avalue,bvalue,cvalue),(2,4,6)):
                            typ=klass.operand_types[name].type
                            if name=='Rc' and width==16:
                                fields[name]='RZ';continue
                            if typ=='Register':fields[name]=reg;regs[f'R{reg}']=value&0xffffffff
                            elif typ=='UniformRegister':fields[name]=10;regs['UR10']=value&0xffffffff
                            elif typ=='C':
                                offset=0x20
                                fields.update({name+'_bank':0,name+'_addr':offset})
                                memory.append(dict(space='cbank',address=offset,
                                                   hex=(value&0xffffffff).to_bytes(4,'little').hex()))
                            elif typ in ('SImm','UImm'):fields[name]=value
                            else:break
                        else:
                            packed=(i2ip_convert(avalue)<<width)|i2ip_convert(bvalue)
                            if width<16:
                                carry_source=cvalue if extract=='H0' else cvalue>>16
                                packed|=(carry_source&((1<<(32-2*width))-1))<<(2*width)
                            add(klass.name,fields,regs,{f'R{rd}':packed&0xffffffff,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='I2IP' and
                  k.name.startswith('i2ip__') and not k.alternate and
                  any(a.type=='CX' for a in k.operand_types.values())):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        if cx_name not in ('Sb','Rb'):continue
        extract_field=next(n for n,x in klass.operand_types.items()
                           if x.type in ('EXTRACT','HSEL','ONLY24','ONLY28'))
        sat_field=next(n for n,x in klass.operand_types.items()
                       if x.type in ('SATONLY','SATRELU','SATRELU_RELU'))
        a,bval,cval=300,-140,0x12345678
        for extract in ('H0','H1'):
            if extract not in arch.enums[klass.operand_types[extract_field].type]:continue
            fields={'Rd':8,'Ra':2,'Rc':6,'dstfmt':'S8','srcfmt':'S32',
                    sat_field:'SAT',extract_field:extract}
            cx_fields,cx_regs,memory=cx_source(klass,cx_name,bval);fields.update(cx_fields)
            # Saturate each input to S8; H0/H1 selects C's preserved high half.
            c_half=(cval if extract=='H0' else cval>>16)&0xffff
            packed=(c_half<<16)|(127<<8)|((-128)&0xff)
            add(klass.name,fields,{'R2':a,'R6':cval,**cx_regs},
                {'R8':packed,'__native':True},memory=memory)
    # CuLifter's immediate UP2UR form packs selected UP0..UP6 bits over URa.
    up2ur=classes.get('up2ur__Imm')
    if up2ur:
        for mask in (0,1,0x55,0x7f,0xff):
            base=0xa5a50000
            predicates={f'UP{bit}':(mask >> bit) & 1 for bit in range(7)}
            packed=base | (mask & 0x7f)
            fields={'UPg':'UPT','URd':8,'UPR':'UPR','URa':4,'Sb':mask,
                    'insert':'B0'}
            add(up2ur.name,fields,{'UR4':base,**predicates},
                {'UR8':packed,'__native':True})
        # A false uniform guard leaves the destination untouched.
        add(up2ur.name,{'UPg':6,'URd':8,'UPR':'UPR','URa':4,'Sb':0x7f,
                        'insert':'B0'},
            {'UR4':0x12345678,'UR8':0xdeadbeef,'UP6':0,
             **{f'UP{i}':1 for i in range(6)}},
            {'UR8':0xdeadbeef,'__native':True})
    # Predicate transfers pack/unpack the seven writable predicate registers.
    # Masks above bit 6 are ignored, and R2P preserves every unselected bit.
    for op in ('P2R','R2P'):
        for klass in (k for k in arch.classes if k.mnemonic==op and not k.alternate):
            mask_name=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
            if (not mask_name or klass.operand_types[mask_name].type in ('C','CX') or
                    any(atom.type in ('C','CX') for atom in klass.operand_types.values())):
                continue
            atom=klass.operand_types[mask_name]
            for mask in (0,1,0x25,0x7f,0xff):
                fields={'Pg':'PT','Ra':4,mask_name:(6 if atom.type in ('Register','UniformRegister') else mask)}
                if atom.type=='Register':registers={mask_name[0]+'6':mask}
                elif atom.type=='UniformRegister':registers={'UR6':mask}
                else:registers={}
                for byte in range(4):
                    if op=='P2R':
                        fields.update(Rd=2,Pr='PR',insert=f'B{byte}')
                        predicates={f'P{i}':((0x55>>i)&1) for i in range(7)}
                        base=0x12345678
                        registers.update(predicates);registers['R4']=base
                        shift=byte*8
                        packed=(0x55 & mask & 0x7f) << shift
                        expected=(base & ~(0xff << shift)) | packed
                        add(klass.name,fields,registers,{'R2':expected,'__native':True})
                    else:
                        fields['a_bsel']=f'B{byte}'
                        initial={f'P{i}':(i&1) for i in range(7)}
                        source=0xa1b2c354
                        registers.update(initial);registers['R4']=source
                        expected={f'P{i}':(((source>>(byte*8+i))&1) if mask&(1<<i) else initial[f'P{i}'])
                                  for i in range(7)}
                        add(klass.name,fields,registers,{**expected,'__native':True})
    # QSPC consumes generic-address provenance from the emulator's explicit
    # map; use model ranges rather than process/GPU virtual addresses.
    qspc=classes.get('qspc__RaNonRZ')
    if qspc:
        qspc_context={'address_spaces':{
            'global':[[0x100000,0x100100]],
            'shared':[[0x200,0x300]],
            'local':[[0x400,0x500]],
            'shared_cluster':[[0x600,0x700]],}}
        e_atom=qspc.operand_types['e']
        e_value='noe' if 'noe' in arch.enums[e_atom.type] else e_atom.default
        for query,base,offset,expected in (
                ('G',0x100020,-32,1),('S',0x220,0,1),
                ('L',0x420,0,1),('G',0x420,0,0)):
            add(qspc.name,dict(Pg='PT',Pu=1,Rd='RZ',e=e_value,
                                space=query,Ra=2,Ra_offset=offset),
                {'R2':base},{'P1':expected},context=qspc_context)
        add(qspc.name,dict(Pg='PT',Pu=1,Rd='RZ',e=e_value,
                           space='G',Ra=2,Ra_offset=0),
            {'R2':0x100000},dict(error='address_spaces.global context required'))
        add(qspc.name,dict(Pg='PT',Pu=1,Rd=8,e=e_value,
                           space='G',Ra=2,Ra_offset=0),
            {'R2':0x100000},{'P1':1,'R8':1},context=qspc_context)
        add(qspc.name,dict(Pg='PT',Pu=1,Rd=8,e=e_value,
                           space='G',Ra=2,Ra_offset=0),
            {'R2':0x420},{'P1':0,'R8':4},context=qspc_context)
        add(qspc.name,dict(Pg='PT',Pu=1,Rd=8,e=e_value,
                           space='G',Ra=2,Ra_offset=0),
            {'R2':0x220},{'P1':0,'R8':2},context=qspc_context)
        space_atom=qspc.operand_types['space']
        if 'D' in arch.enums[space_atom.type]:
            add(qspc.name,dict(Pg='PT',Pu=1,Rd='RZ',e=e_value,
                               space='D',Ra=2,Ra_offset=0),
                {'R2':0x620},{'P1':1},context=qspc_context)
            add(qspc.name,dict(Pg='PT',Pu=1,Rd='RZ',e=e_value,
                               space='D',Ra=2,Ra_offset=0),
                {'R2':0x420},{'P1':0},context=qspc_context)
            add(qspc.name,dict(Pg='PT',Pu=1,Rd=8,e=e_value,
                               space='D',Ra=2,Ra_offset=0),
                {'R2':0x620},dict(error='sass_opaque_QSPC'),
                context=qspc_context)
    qspc_urb=classes.get('qspc_urb__Ra32')
    if qspc_urb:
        e_atom=qspc_urb.operand_types['e']
        e_value='noe' if 'noe' in arch.enums[e_atom.type] else e_atom.default
        fields=dict(Pg='PT',Pu=1,Rd='RZ',e=e_value,space='S',Ra=2,
                    input_reg_sz_32_dist='U32',Ra_URb=4,Ra_offset=0)
        add(qspc_urb.name,fields,{'R2':0x100,'UR4':0x120},{'P1':1},
            context=qspc_context)
    add('ldc__RaNonRZ',dict(Rd=8,Ra=2,Ra_offset=16,Sa_bank=3),dict(R2=0x100),dict(R8=0x12345678),memory=[dict(space='cbank',address=(3<<32)|0x110,hex='78563412')])
    for stride,factor in [('X4',4),('X8',8),('X16',16)]:
        add('lds__sImmOffset',dict(Rd=8,Ra=2,Ra_offset=4,stride=stride),dict(R2=0x10),
            dict(R8=0x12345678),memory=[dict(space='shared',address=0x10*factor+4,hex='78563412')])
    add('cs2r_',dict(Rd=8,SRa=0,sz='64'),dict(SR_LANEID=123,SR_CLOCK=4),dict(R8=123,R9=4))
    add('cs2r_',dict(Rd=8,SRa='SRZ',sz='64'),dict(R8=1,R9=2),dict(R8=0,R9=0))
    add('fsetp__RRR_RRR',dict(Pu=0,Pv=1,Pp=2,Ra=2,Rb=4,fcomp='LT',bop='AND'),dict(R2=0x3f800000,R4=0x40000000,P2=1),dict(P0=1,P1=0))
    add('fsetp__RRR_RRR',dict(Pu=0,Pv=1,Pp=0,Ra=2,Rb=4,fcomp='NAN',bop='AND'),dict(R2=0x7fc00000,R4=0,P0=1),dict(P0=1,P1=0))
    add('i2f__IS_64b',dict(Rd=8,Sb=-1,dstfmt='F32'),{},dict(R8=0xbf800000))
    add('f2i__Ib_16b',dict(Rd=8,Sb=0x3e00,rnd='TRUNC'),{},dict(R8=1))
    add('f2i__Ib_64b',dict(Rd=8,Sb=0x40080000,rnd='TRUNC'),{},dict(R8=3))
    add('bra_',dict(sImm=32),{},dict(__counter=0x1030))
    add('bra_',dict(sImm=32,Pp=0),dict(P0=0),dict(__counter=0x1010))
    add('call_abs__RIR',dict(Sa=0x4560,depth='NOINC'),{},dict(__counter=0x4560))
    add('ret__ABS',dict(Ra=2,Ra_offset=0,depth='NODEC'),dict(R2=0x4560,R3=0),dict(__counter=0x4560))
    add('exit_',{}, {},dict(__counter=0))
    for op,space in [('stg','ram'),('sts','shared'),('stl','localmem')]:
        add(op+'__sImmOffset',dict(Ra=2,Ra_offset=0,Rb=4,sz='32'),dict(R2=0x100,R3=0,R4=0x12345678),dict(__memory='78563412'),observe_memory=[dict(space=space,address=0x100,size=4)])
    warp=dict(values=list(range(32)),predicates=[i%2 for i in range(32)],lane=3,active=0xffffffff)
    add('shfl__RRR',dict(Rd=8,Pu=0,Ra=2,Rb=4,Rc=6,shflmd='IDX'),dict(R2=3,R4=5,R6=31),dict(R8=5,P0=1),context={'warp':warp})
    add('shfl__RRR',dict(Rd=8,Pu=0,Ra=2,Rb=4,Rc=6,shflmd='IDX'),dict(R2=3,R4=5,R6=31),dict(error='inactive shuffle source'),context={'warp':dict(warp,active=0xffffffdf)})
    add('bssy_',dict(Pp=0),dict(P0=0),dict(__events=0))
    add('vote_',dict(Rd=8,Pu=0,Pp=1,voteop='ANY'),dict(P1=1),dict(R8=0xaaaaaaaa,P0=1),context={'warp':warp})
    add('mufu__RRR_RR',dict(Rd=8,Rb=2),dict(R2=0x3f800000),dict(error='MUFU approximation'))
    add('bar__SYNCALL_noSrc_II',{}, {},dict(__events=1),context={'synchronization':{'bar':True}})
    add('bssy_',{}, {},dict(error='synchronization context required'))
    add('bsync_',{}, {},dict(__events=1),context={'synchronization':{'bsync':True}})
    add('mov__RI',dict(Rd=3,Sb=5,PixMaskU04=1),{},dict(error='sass_opaque_MOV'))
    # Native fp32 p-code (every rounding mode, .FTZ, .SAT, FMUL scale) against
    # the exact softfloat model.
    def nan32(x):return x&0x7f800000==0x7f800000 and x&0x7fffff
    def ghidra_underflow(op,inputs,k=0):
        # Ghidra 12.1.4 FloatFormat.getEncoding returns zero when a result lies
        # strictly between half and one minimum subnormal (its "XXX ... round up"
        # branch); IEEE round-to-nearest gives the minimum subnormal.
        from fractions import Fraction
        parts=[softfloat.unpack(x) for x in inputs]
        if any(kind!='finite' and kind!='zero' for kind,_,_ in parts):return False
        a,b=parts[0][2],parts[1][2];v=abs(a*b*Fraction(2)**k+(parts[2][2] if op=='fma' else 0) if op!='add' else a+b)
        return Fraction(1,2)*Fraction(2)**-149<v<Fraction(2)**-149
    edges=[0,0x80000000,1,0x80000001,0x007fffff,0x00800000,0x3f800000,0xbf800000,0x7f7fffff,0xff7fffff,0x7f800000,0xff800000,0x3effffff,0x4b800001]
    rng=random.Random('fp32')
    floats=edges+[rng.getrandbits(32)&0xbfffffff for _ in range(10)]+[rng.getrandbits(32) for _ in range(10)]
    vectors=[(floats[i],floats[(i*7+3)%len(floats)],floats[(i*5+1)%len(floats)]) for i in range(len(floats))]
    for x,y,z in vectors:
        for rnd in ('RN','RZ','RM','RP'):
            for flush in (False,True):
                for sat in (False,True):
                    extra=dict(rnd=rnd,sat='SAT' if sat else 'nosat')
                    for op,cls,fields,inputs in [('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,**({'ftz':'FTZ'} if flush else {})),[x,y]),
                                                 ('mul','fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4,**({'fmz':'FTZ'} if flush else {})),[x,y]),
                                                 ('fma','ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,**({'fmz':'FTZ'} if flush else {})),[x,y,z])]:
                        want=softfloat.arithmetic(op,inputs,32,rnd,flush,sat)
                        if nan32(want) or (rnd=='RN' and ghidra_underflow(op,inputs)):continue
                        add(cls,dict(fields,**extra),dict(R2=x,R4=y,R6=z),dict(R8=want,__native=True))
    for scale,k in [('D2',-1),('D8',-3),('M2',1),('M8',3)]:
        for x,y,_ in vectors:
          for rnd in ('RN','RZ','RM','RP'):
            want=softfloat.arithmetic('mul',[x,y],32,rnd,False,False,k)
            if not (nan32(want) or rnd=='RN' and ghidra_underflow('mul',[x,y],k)):
                add('fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4,scale=scale,rnd=rnd),dict(R2=x,R4=y),dict(R8=want,__native=True))
    add('fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),dict(R2=0x3f800000,R4=0x40000000),dict(R8=0x40400000,__max_ops=2))
    add('fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,rnd='RZ'),dict(R2=0x3f800000,R4=0x33800001),dict(R8=0x3f800000))
    # Directed fp32 cases whose residual lies below fp64 precision, and exact
    # cancellations whose zero sign depends on the rounding mode.
    for op,cls,fields,inputs in [
        ('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),[0x3f800000,1]),
        ('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),[0x3f800000,0x80000001]),
        ('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),[0x7f7fffff,0x7f7fffff]),
        ('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),[0x3f800000,0xbf800000]),
        ('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4),[0,0x80000000]),
        ('fma','ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6),[0x3f800000,0x3f800000,0xbf800000]),
        ('fma','ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6),[0x3f800001,0x3f800001,0x80000001]),
        ('fma','ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6),[0x80000000,0x3f800000,0]),
        ('mul','fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4),[0x00800000,0x3f000001])]:
        for rnd in ('RN','RZ','RM','RP'):
            add(cls,dict(fields,rnd=rnd),dict(zip(('R2','R4','R6'),inputs)),
                dict(R8=softfloat.arithmetic(op,inputs,32,rnd),__native=True))
    for a,c in [(0x3f800000,0x40000000),(0x40000000,0x3f800000),(0x3f800000,0x3f800000),(0x7fc00000,0x3f800000),(0x3f800000,0xffc00000),(0x00000001,0)]:
        for flush in (False,True):
            fa,fc=(softfloat.unpack(v,32,flush) for v in (a,c))
            unordered=fa[0]=='nan' or fc[0]=='nan'
            if not unordered:fa,fc=fa[2],fc[2]
            base={'LT':lambda:fa<fc,'EQ':lambda:fa==fc,'LE':lambda:fa<=fc,'GT':lambda:fa>fc,'NE':lambda:fa!=fc,'GE':lambda:fa>=fc}
            for cmp in ['LT','EQ','LE','GT','NE','GE','LTU','EQU','LEU','GTU','NEU','GEU','NUM','NAN','T','F']:
                b0=cmp.removesuffix('U')
                result=(True if cmp=='T' else False if cmp=='F' else not unordered if cmp=='NUM' else unordered if cmp=='NAN'
                        else (unordered and cmp.endswith('U')) or (not unordered and base[b0]()))
                add('fsetp__RRR_RRR',dict(Pu=0,Pv=1,Pp='PT',Ra=2,Rb=4,fcomp=cmp,bop='AND',**({'ftz':'FTZ'} if flush else {})),
                    dict(R2=a,R4=c),dict(P0=int(result),P1=int(not result)))
    # FP64 pairs, overlap, modifiers, and exact rational arithmetic references.
    def pair(name,bits):
        prefix='UR' if name.startswith('UR') else 'R';index=int(name[len(prefix):])
        return {name:bits&0xffffffff,f'{prefix}{index+1}':bits>>32}
    edges64=[0,1<<63,1,(1<<63)|1,0x000fffffffffffff,0x0010000000000000,
             0x3ff0000000000000,0xbff0000000000000,0x7fefffffffffffff,
             0x7ff0000000000000,0xfff0000000000000,0x3ff0000000000001]
    rng=random.Random('fp64')
    values64=edges64+[rng.getrandbits(64) for _ in range(30)]
    vectors64=[(v,values64[(i*7+3)%len(values64)],values64[(i*5+1)%len(values64)]) for i,v in enumerate(values64)]
    # This cancellation needs an exact wide product, rather than fp64 multiply/add.
    vectors64 += [(0x3ff0000000000001,0x3feffffffffffffe,0xbff0000000000000),
                  (0x7fefffffffffffff,0x4000000000000000,0xffefffffffffffff)]
    from fractions import Fraction
    for i,(x,y,z) in enumerate(vectors64):
        regs={**pair('R2',x),**pair('R4',y),**pair('R6',z)}
        for op,cls,fields,inputs in [
            ('add','dadd__RRR_RR',dict(Rd=2,Ra=2,Rc=4),[x,y]),
            ('mul','dmul__RRR_RR',dict(Rd=2,Ra=2,Rb=4),[x,y]),
            ('fma','dfma__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6),[x,y,z])]:
            if i%3==0:
                fields.update({'Ra@absolute':1,'Ra@negate':1})
                inputs[0]=(inputs[0]&((1<<63)-1))|(1<<63)
            for rnd in ('RN','RZ','RM','RP'):
                want=softfloat.arithmetic(op,inputs,64,rnd)
                kind=softfloat.unpack(want,64)[0]
                if kind=='nan':continue
                parts=[softfloat.unpack(v,64) for v in inputs]
                if rnd=='RN' and all(p[0]=='finite' for p in parts):
                    exact=abs(parts[0][2]+parts[1][2] if op=='add' else
                              parts[0][2]*parts[1][2]+(parts[2][2] if op=='fma' else 0))
                    if Fraction(2)**-1075<exact<Fraction(2)**-1074:continue # Ghidra underflow bug
                add(cls,dict(fields,rnd=rnd),regs,dict(pair('R2',want),__native=True))
    # Directed rounding: residual carries the sign below the wide sum's precision,
    # and an exact cancellation is -0 under RM.
    for op,cls,fields,inputs in [
        ('add','dadd__RRR_RR',dict(Rd=2,Ra=2,Rc=4),[0x3ff0000000000000,1]),
        ('add','dadd__RRR_RR',dict(Rd=2,Ra=2,Rc=4),[0x3ff0000000000000,(1<<63)|1]),
        ('add','dadd__RRR_RR',dict(Rd=2,Ra=2,Rc=4),[0x3ff0000000000000,0xbff0000000000000]),
        ('fma','dfma__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6),[0x3ff0000000000000,0x3ff0000000000000,0xbff0000000000000]),
        ('fma','dfma__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6),[0x3ff0000000000001,0x3ff0000000000001,(1<<63)|1])]:
        regs={**pair('R2',inputs[0]),**pair('R4',inputs[1]),**pair('R6',inputs[-1])}
        for rnd in ('RN','RZ','RM','RP'):
            add(cls,dict(fields,rnd=rnd),regs,dict(pair('R2',softfloat.arithmetic(op,inputs,64,rnd)),__native=True))
    for cls,fields,regs in [
        ('dadd__RRU_RU',dict(Rd=8,Ra=2,URc=4),{**pair('R2',0x3ff0000000000000),**pair('UR4',0x4000000000000000)}),
        ('dadd__RRsI_RI',dict(Rd=8,Ra=2,Sc=0x40000000),pair('R2',0x3ff0000000000000)),
        ('dadd__RRC_RC',dict(Rd=8,Ra=2,Sc_bank=0,Sc_addr=0x100),pair('R2',0x3ff0000000000000))]:
        add(cls,fields,regs,dict(pair('R8',0x4008000000000000),__native=True),
            memory=[dict(space='cbank',address=0x100,hex='0000000000000040')])
    for cls,fields in [
        ('dmul__RCR_RC',dict(Rd=8,Ra=2,Sb_bank=0,Sb_addr=0x100)),
        ('dfma__RCR_RCR',dict(Rd=8,Ra=2,Sb_bank=0,Sb_addr=0x100,Rc=4))]:
        result=0x4008000000000000 if cls.startswith('dfma') else 0x4000000000000000
        add(cls,fields,{**pair('R2',0x3ff0000000000000),**pair('R4',0x3ff0000000000000)},
            dict(pair('R8',result),__native=True),memory=[dict(space='cbank',address=0x100,hex='0000000000000040')])
    add('dadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,Pg=0),dict(pair('R2',0x3ff0000000000000),P0=0,R8=7,R9=9),dict(R8=7,R9=9))
    add('dadd__RRR_RR',dict(Rd=8,Ra='RZ',Rc=4),pair('R4',0x4000000000000000),dict(pair('R8',0x4000000000000000),__native=True))
    add('dadd__RRR_RR',dict(Rd=8,Ra=254,Rc=4),{},dict(error='sass_opaque_DADD'))
    add('dsetp__RRC_RC',dict(Pu=0,Pv=1,Pp='PT',Ra=2,Sc_bank=0,Sc_addr=0x100,test='LT',bop='AND'),
        pair('R2',0x3ff0000000000000),dict(P0=1,P1=0,__native=True),memory=[dict(space='cbank',address=0x100,hex='0000000000000040')])
    import math,struct
    for a,c in [(0,1<<63),(1,0),(0x7ff0000000000000,0x3ff0000000000000),
                (0x7ff8000000000000,0),(0,0x7ff0000000000001),(0x3ff0000000000000,0x4000000000000000)]:
        fa,fc=[struct.unpack('<d',v.to_bytes(8,'little'))[0] for v in (a,c)]
        unordered=math.isnan(fa) or math.isnan(fc)
        base={'LT':fa<fc,'EQ':fa==fc,'LE':fa<=fc,'GT':fa>fc,'NE':fa!=fc,'GE':fa>=fc}
        for cmp in ['LT','EQ','LE','GT','NE','GE','LTU','EQU','LEU','GTU','NEU','GEU','NUM','NAN']:
            result=not unordered if cmp=='NUM' else unordered if cmp=='NAN' else (unordered and cmp.endswith('U')) or (not unordered and base[cmp.removesuffix('U')])
            for bop in ('AND','OR','XOR'):
                for p in (0,1):
                    combine=lambda v:(v&p) if bop=='AND' else (v|p) if bop=='OR' else (v^p)
                    add('dsetp__RRR_RR',dict(Pu=0,Pv=1,Pp=0,Ra=2,Rc=4,test=cmp,bop=bop),
                        dict(pair('R2',a),**pair('R4',c),P0=p),dict(P0=combine(int(result)),P1=combine(int(not result)),__native=True))
    for cmp in ('MIN','MAX'):
        add('dsetp__RRR_RR',dict(test=cmp),{},dict(error='sass_opaque_DSETP'))
    # Packed FP16 lanes: modifiers act on both sign bits before broadcasting.
    def half_lanes(value,swizzle='H1_H0',absolute=False,negate=False):
        if absolute:value &= 0x7fff7fff
        if negate:value ^= 0x80008000
        lo,hi=value&0xffff,value>>16
        return [lo,lo] if swizzle=='H0_H0' else [hi,hi] if swizzle=='H1_H1' else [lo,hi]
    def half_reference(op,inputs,sat=False):
        result=0
        for lane in (0,1):
            xs=[v[lane] for v in inputs];want=softfloat.arithmetic(op,xs,16,sat=sat)
            if softfloat.unpack(want,16)[0]=='nan':return None
            parts=[softfloat.unpack(v,16) for v in xs]
            if not sat and all(v[0] in ('zero','finite') for v in parts):
                exact=abs(parts[0][2]+parts[1][2] if op=='add' else parts[0][2]*parts[1][2]+parts[2][2])
                if Fraction(2)**-25<exact<Fraction(2)**-24:return None # Ghidra minimum-subnormal bug
            result |= want<<(16*lane)
        return result
    packed=[0,0x80008000,0x00010001,0x80010001,0x03ff0400,0x3c004000,
            0xbc00c000,0x7bfffbff,0x7c003c00,0x3c007e00,0x3c014001]
    rng=random.Random('fp16')
    packed += [rng.getrandbits(32) for _ in range(15)]
    for i,x in enumerate(packed):
        y,z=packed[(i*7+3)%len(packed)],packed[(i*5+1)%len(packed)]
        for swizzle in ('H1_H0','H0_H0','H1_H1'):
            for absolute,negate in ((0,0),(1,0),(0,1),(1,1)):
                for sat in (False,True):
                    for op,cls,fields,inputs in [
                        ('add','hadd2__RR',dict(Rd=2,Ra=2,Rc=4,iswzA=swizzle,iswzB_as_C=swizzle,sat='SAT' if sat else 'nosat'),
                         [half_lanes(x,swizzle,absolute,negate),half_lanes(y,swizzle)]),
                        ('fma','hfma2__RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6,iswzA=swizzle,iswzB=swizzle,iswzC=swizzle,
                          **{('satrelu' if 'satrelu' in classes['hfma2__RRR'].operand_types else 'sat'):'SAT' if sat else 'nosat'}),
                         [half_lanes(x,swizzle,absolute,negate),half_lanes(y,swizzle),half_lanes(z,swizzle)])]:
                        fields.update({'Ra@absolute':absolute,'Ra@negate':negate})
                        want=half_reference(op,inputs,sat)
                        if want is not None:add(cls,fields,dict(R2=x,R4=y,R6=z),dict(R2=want,__native=True))
    # Distinct broadcasts on each input and asymmetric immediate/constant lanes.
    from itertools import product
    x,y,z=0x3c004000,0x42004400,0x38003c00
    for a,b,c in product(('H1_H0','H0_H0','H1_H1'),repeat=3):
        want=half_reference('fma',[half_lanes(x,a),half_lanes(y,b),half_lanes(z,c)])
        add('hfma2__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,iswzA=a,iswzB=b,iswzC=c),dict(R2=x,R4=y,R6=z),dict(R8=want,__native=True))
    for cls,fields,inputs in [
        ('hadd2__RI',dict(Rd=8,Ra=2,Sc=y>>16,Sc1=y&0xffff),[half_lanes(x),half_lanes(y)]),
        ('hfma2__RIR',dict(Rd=8,Ra=2,Sb=y>>16,Sb1=y&0xffff,Rc=6),[half_lanes(x),half_lanes(y),half_lanes(z)]),
        ('hfma2__RRI',dict(Rd=8,Ra=2,Rb=4,Sc=z>>16,Sc1=z&0xffff),[half_lanes(x),half_lanes(y),half_lanes(z)]),
        ('hadd2__RC',dict(Rd=8,Ra=2,Sc_bank=0,Sc_addr=0x100),[half_lanes(x),half_lanes(y)]),
        ('hfma2__RCR',dict(Rd=8,Ra=2,Sb_bank=0,Sb_addr=0x100,Rc=6),[half_lanes(x),half_lanes(y),half_lanes(z)]),
        ('hfma2__RRC',dict(Rd=8,Ra=2,Rb=4,Sc_bank=0,Sc_addr=0x104),[half_lanes(x),half_lanes(y),half_lanes(z)]),
        ('hadd2__RU',dict(Rd=8,Ra=2,URc=4),[half_lanes(x),half_lanes(y)]),
        ('hfma2__RRU',dict(Rd=8,Ra=2,Rb=4,URc=6),[half_lanes(x),half_lanes(y),half_lanes(z)])]:
        want=half_reference('fma' if cls.startswith('hfma') else 'add',inputs)
        add(cls,fields,dict(R2=x,R4=y,R6=z,UR4=y,UR6=z),dict(R8=want,__native=True),
            memory=[dict(space='cbank',address=0x100,hex=(y.to_bytes(4,'little')+z.to_bytes(4,'little')).hex())])
    # HMUL2 is a packed binary16 multiply. Exercise each operand encoding,
    # lane broadcast, source modifiers and overlap with the destination.
    mul_values=[0x3c004000,0x00010001,0x80010001,0x7bff3c00,0x040003ff,
                0xbc00c000,0x35553aaa]
    for klass in (k for k in arch.classes if k.mnemonic=='HMUL2' and not k.alternate):
        src=next((n for n in ('Rb','Sb','URb') if n in klass.operand_types),None)
        if not src:continue
        atom=klass.operand_types[src]
        for i,x in enumerate(mul_values):
            y=mul_values[(i*3+2)%len(mul_values)]
            for sa,sb in (('H1_H0','H1_H0'),('H0_H0','H1_H1'),('H1_H1','H0_H0')):
                effective_sb=sb if 'iswzB' in klass.operand_types else 'H1_H0'
                fields={'Rd':2,'Ra':2 if i%2==0 else 4,'iswzA':sa,'ofmt':'F16_V2',
                        'fmz':klass.operand_types['fmz'].default,'sat':'nosat'}
                fields.update({'Ra@absolute':int(bool(i&1)),'Ra@negate':int(bool(i&2))})
                regs={'R2':x,'R4':x}
                memory=[]
                if src=='Rb':
                    fields.update(Rb=6,iswzB=sb);regs['R6']=y
                elif atom.type=='UniformRegister':
                    fields.update(URb=6,iswzB=sb);regs['UR6']=y
                elif atom.type=='C':
                    fields.update(Sb_bank=0,Sb_addr=0x100,iswzB=sb)
                    memory=[dict(space='cbank',address=0x100,hex=y.to_bytes(4,'little').hex())]
                elif atom.type in ('F16Imm','UImm'):
                    fields.update(Sb=y>>16,Sb1=y&0xffff)
                else:continue
                lanes_a=half_lanes(x,sa,bool(i&1),bool(i&2))
                lanes_b=half_lanes(y,effective_sb)
                want=sum(softfloat.arithmetic('mul',[lanes_a[lane],lanes_b[lane]],16)<<(16*lane)
                         for lane in (0,1))
                add(klass.name,fields,regs,dict(R2=want,__native=True),memory=memory)
    add('hadd2__RR',dict(Rd=8,Ra=2,Rc=4,ftz='FTZ'),{},dict(error='sass_opaque_HADD2'))
    add('hfma2__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmz='FMZ'),{},dict(error='sass_opaque_HFMA2'))
    def hmul2_reference(a,b,mode,sat):
        lanes=[]
        for shift in (0,16):
            x,y=(a>>shift)&0xffff,(b>>shift)&0xffff
            if fp=='exact' and mode=='FMZ' and ((x&0x7c00)==0 or (y&0x7c00)==0):
                # SM89 HMUL2.FMZ returns canonical +0 if either source is
                # zero/subnormal, even for zero*Inf or signed-zero products.
                lanes.append(0)
                continue
            if fp=='exact' and (x&0x7c00)==0 and (x&0x03ff) and mode=='FMZ':x=0
            if fp=='exact' and (y&0x7c00)==0 and (y&0x03ff) and mode=='FMZ':y=0
            result=softfloat.arithmetic('mul',[x,y],16,ftz=(fp=='exact' and mode in ('FMZ','FTZ')),sat=sat)
            lanes.append(result)
        return lanes[0]|(lanes[1]<<16)
    if 'hmul2__RR' in classes:
        for mode in ('FMZ','FTZ'):
            for sat in (False,True):
                for a,b in ((0x00010001,0x60006000),(0x80018001,0x60006000),
                            (0x14001400,0x14001400),(0x94009400,0x14001400),
                            (0x7e017c00,0x3c003c00),(0xbc00c000,0x40004000)):
                    add('hmul2__RR',dict(Rd=8,Ra=2,Rb=4,fmz=mode,sat='SAT' if sat else 'nosat'),
                        dict(R2=a,R4=b),dict(R8=hmul2_reference(a,b,mode,sat),__native=True))
    for klass in (k for k in arch.classes if k.mnemonic=='HADD2' and
                  k.name=='hadd2__RCx' and not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        fields={'Rd':8,'Ra':2}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
        add(klass.name,fields,{'R2':0x3c003c00,**cx_regs},
            {'R8':0x42004200,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='HFMA2' and
                  k.name=='hfma2__RCxR' and not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        fields={'Rd':8,'Ra':2,'Rc':6}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
        add(klass.name,fields,{'R2':0x3c003c00,'R6':0x42004200,**cx_regs},
            {'R8':0x45004500,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='HMUL2' and
                  k.name=='hmul2__RCx' and not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        fields={'Rd':8,'Ra':2}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
        add(klass.name,fields,{'R2':0x3c003c00,**cx_regs},
            {'R8':0x40004000,'__native':True},memory=memory)
    for klass in (k for k in arch.classes if k.mnemonic=='HMNMX2' and
                  k.name=='hmnmx2__RCxR' and not k.alternate):
        cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
        fields={'Rd':8,'Ra':2,'Pp':'PT'}
        cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
        add(klass.name,fields,{'R2':0x3c003c00,**cx_regs},
            {'R8':0x3c003c00,'__native':True},memory=memory)
    if 'hmnmx2__RRR' in classes:
        def half_minmax(a,b,is_min,nan_mode,ftz,xorsign,abs_a=None,abs_b=None,bf16=False,e6m9=False):
            if abs_a is None:abs_a=xorsign
            if abs_b is None:abs_b=xorsign
            lanes=[]
            for shift in (0,16):
                x=(a>>shift)&0xffff;y=(b>>shift)&0xffff
                sx=(x>>15)&1;sy=(y>>15)&1
                if abs_a:x&=0x7fff
                if abs_b:y&=0x7fff
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
                        sign_x=(x&0x8000)!=0;sign_y=(y&0x8000)!=0
                        xm=x&0x7fff;ym=y&0x7fff
                        less=(sign_x if sign_x!=sign_y else (xm>ym if sign_x else xm<ym))
                        r=x if less==is_min else y
                    else:
                        if bf16:
                            xf=struct.unpack('<f',(x<<16).to_bytes(4,'little'))[0]
                            yf=struct.unpack('<f',(y<<16).to_bytes(4,'little'))[0]
                        else:
                            xf=struct.unpack('<e',x.to_bytes(2,'little'))[0]
                            yf=struct.unpack('<e',y.to_bytes(2,'little'))[0]
                        if xf==yf==0:r=(x|y) if is_min else (x&y)
                        else:r=x if (xf<yf)==is_min else y
                if xorsign and not ((xn and yn) or (nan_mode and (xn or yn))):
                    r=(r&0x7fff)|((sx^sy)<<15)
                if ftz and r&0x7c00==0:r&=0x8000
                lanes.append(r)
            return lanes[0]|(lanes[1]<<16)
        cases_hmn=[(0x3c003c00,0xc000c000),(0x80000000,0x00000000),
                   (0x7e017c00,0x40003c00),(0x00010001,0x80018001),
                   (0xbc003c00,0x4200c000),(0xffc17fc1,0x7fc17fc1)]
        hmn_has_xor='xorsign' in classes['hmnmx2__RRR'].operand_types
        for a,bval in cases_hmn:
            for is_min,nan_mode,ftz in itertools.product((False,True),(False,True),(False,True)):
                # XORSIGN encodings require both input absolute modifiers.
                use_xor=(hmn_has_xor and a==0x3c003c00 and bval==0xc000c000 and
                         is_min and not nan_mode and not ftz)
                fields=dict(Rd=8,Ra=2,Rb=4,Pp='PT',**{'Pp@not':int(not is_min)},
                            nan='NAN' if nan_mode else 'nonan',ftz='FTZ' if ftz else 'noftz',
                            iswzA='H1_H0',iswzB='H1_H0')
                if use_xor:
                    fields.update(xorsign='XORSIGN',**{'Ra@absolute':1,'Rb@absolute':1})
                elif hmn_has_xor:fields['xorsign']='noxorsign'
                add('hmnmx2__RRR',fields,{'R2':a,'R4':bval},
                    {'R8':half_minmax(a,bval,is_min,nan_mode,ftz,use_xor),'__native':True})
        if hmn_has_xor:
            for fmt, a, bval, bf16, e6m9 in (
                ('F16_V2',0x80010000,0x7e017e01,False,False),
                ('BF16_V2',0x80010000,0x7fc17fc1,True,False),
                ('E6M9_V2',0x80010000,0x7e017e01,False,True)):
                if fmt not in arch.enums[classes['hmnmx2__RRR'].operand_types['ofmt'].type]:
                    continue
                for is_min in (False,True):
                    fields=dict(Rd=8,Ra=2,Rb=4,Pp='PT',**{'Pp@not':int(not is_min)},
                        ofmt=fmt,nan='nonan',ftz='noftz',xorsign='XORSIGN',
                        iswzA='H1_H0',iswzB='H1_H0',
                        **{'Ra@absolute':1,'Rb@absolute':1})
                    add('hmnmx2__RRR',fields,{'R2':a,'R4':bval},
                        {'R8':half_minmax(a,bval,is_min,False,False,True,
                            bf16=bf16,e6m9=e6m9),'__native':True})
        if 'BF16_V2' in arch.enums[classes['hmnmx2__RRR'].operand_types['ofmt'].type]:
            xor_supported=('xorsign' in classes['hmnmx2__RRR'].operand_types and
                'XORSIGN' in arch.enums[classes['hmnmx2__RRR'].operand_types['xorsign'].type])
            for a,bval in ((0x3f803f80,0xc000c000),(0,0x80008000),
                           (0x7fc17f80,0x40003f80),(0x7f80ff80,0x7f7fff7f),
                           (0xbf803f80,0x4200c000),(0xffc17fc1,0x7fc17fc1)):
                for is_min,nan_mode,use_xor in itertools.product((False,True),(False,True),
                                                                  (False,True) if xor_supported else (False,)):
                    if use_xor and (nan_mode or not is_min or a not in (0x3f803f80,0xbf803f80)):
                        continue
                    fields=dict(Rd=8,Ra=2,Rb=4,Pp='PT',**{'Pp@not':int(not is_min)},
                        ofmt='BF16_V2',nan='NAN' if nan_mode else 'nonan',ftz='noftz',
                        iswzA='H1_H0',iswzB='H1_H0')
                    if use_xor:fields.update(xorsign='XORSIGN',**{'Ra@absolute':1,'Rb@absolute':1})
                    elif xor_supported:fields['xorsign']='noxorsign'
                    add('hmnmx2__RRR',fields,{'R2':a,'R4':bval},
                        {'R8':half_minmax(a,bval,is_min,nan_mode,False,use_xor,bf16=True),
                         '__native':True})
        if 'E6M9_V2' in arch.enums[classes['hmnmx2__RRR'].operand_types['ofmt'].type]:
            xor_supported=('xorsign' in classes['hmnmx2__RRR'].operand_types and
                'XORSIGN' in arch.enums[classes['hmnmx2__RRR'].operand_types['xorsign'].type])
            for a,bval in ((0x40004000,0x3f803f80),(0,0x80008000),
                           (0x7fff7fff,0x3f803f80),(0x7e00fe00,0x3f80bf80),
                           (0x00010001,0x80028002),(0xc0003f80,0x41003e00),
                           (0xffff7fff,0x7fff7fff)):
                for is_min,nan_mode,use_xor in itertools.product((False,True),(False,True),
                                                                  (False,True) if xor_supported else (False,)):
                    if use_xor and (nan_mode or not is_min):continue
                    fields=dict(Rd=8,Ra=2,Rb=4,Pp='PT',**{'Pp@not':int(not is_min)},
                        ofmt='E6M9_V2',nan='NAN' if nan_mode else 'nonan',ftz='noftz',
                        iswzA='H1_H0',iswzB='H1_H0')
                    if use_xor:fields.update(xorsign='XORSIGN',**{'Ra@absolute':1,'Rb@absolute':1})
                    elif xor_supported:fields['xorsign']='noxorsign'
                    add('hmnmx2__RRR',fields,{'R2':a,'R4':bval},
                        {'R8':half_minmax(a,bval,is_min,nan_mode,False,use_xor,e6m9=True),
                         '__native':True})
        if 'hmnmx2__RIR' in classes:
            for swizzle in ('H1_H0','H0_H0','H1_H1'):
                a=0x40003c00
                bval=0x42003e00
                fields=dict(Rd=8,Ra=2,Sb=0x4200,Sb1=0x3e00,Pp='PT',
                    iswzA=swizzle,nan='nonan',ftz='noftz')
                if 'xorsign' in classes['hmnmx2__RIR'].operand_types:fields['xorsign']='noxorsign'
                if swizzle=='H0_H0':a=(a&0xffff)*0x10001
                if swizzle=='H1_H1':a=((a>>16)&0xffff)*0x10001
                add('hmnmx2__RIR',fields,{'R2':0x40003c00},
                    {'R8':half_minmax(a,bval,True,False,False,False),'__native':True})
            if 'xorsign' in classes['hmnmx2__RIR'].operand_types:
                immediate=0x3c00
                for src,abs_a in ((0xc000c000,0),(0xc000c000,1),
                                  (0x40004000,0),(0x40004000,1),
                                  (0xbc00bc00,1)):
                    fields=dict(Rd=8,Ra=2,Sb=immediate,Sb1=immediate,Pp='PT',
                        iswzA='H1_H0',nan='nonan',ftz='noftz',xorsign='XORSIGN',
                        **{'Ra@absolute':abs_a})
                    add('hmnmx2__RIR',fields,{'R2':src},
                        {'R8':half_minmax(src,immediate|(immediate<<16),True,
                            False,False,True,abs_a,False),'__native':True})
        for cls in ('hmnmx2__RUR','hmnmx2__RCR'):
            if cls not in classes:continue
            a,bval=0x40003c00,0x42003e00
            fields=dict(Rd=8,Ra=2,Pp='PT',iswzA='H1_H0',iswzB='H1_H0',
                        nan='nonan',ftz='noftz')
            if 'xorsign' in classes[cls].operand_types:fields['xorsign']='noxorsign'
            regs={'R2':a};extra={}
            if cls=='hmnmx2__RUR':fields['URb']=4;regs['UR4']=bval
            else:
                fields.update(Sb_bank=0,Sb_addr=0x100)
                extra['memory']=[dict(space='cbank',address=0x100,
                                      hex=bval.to_bytes(4,'little').hex())]
            add(cls,fields,regs,{'R8':half_minmax(a,bval,True,False,False,False),'__native':True},**extra)
    # NVIDIA's HMNMX2 patent describes Pu/Pv as per-lane source indicators.
    # The IS_A encoding enables those outputs; true denotes source A.
    for klass in (k for k in arch.classes if k.mnemonic=='HMNMX2' and
                  k.name.startswith('hmnmx2_pred__') and not k.alternate):
        source=next((n for n,a in klass.operand_types.items() if a.type=='CX'),None)
        if source is None:
            source=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if source is None or 'IS_A' not in arch.enums[klass.operand_types['isA'].type]:continue
        a=0x44003e00 # low A=1.5, high A=4.0
        bval=0x42004000 # low B=2.0, high B=3.0
        atom=klass.operand_types[source];regs={'R2':a};extra={}
        fields=dict(Rd=8,Ra=2,Pu=2,Pv=3,isA='IS_A',ofmt='F16_V2',
            ftz='noftz',nan='nonan',Pp='PT',iswzA='H1_H0')
        if 'iswzB' in klass.operand_types:fields['iswzB']='H1_H0'
        if 'xorsign' in klass.operand_types:fields['xorsign']='noxorsign'
        if atom.type=='Register':fields[source]=4;regs['R4']=bval
        elif atom.type=='UniformRegister':fields[source]=4;regs['UR4']=bval
        elif atom.type=='F32Imm':fields[source]=bval
        elif atom.type=='F64Imm':
            fields[source]=(bval>>16)&0xffff;fields[source+'1']=bval&0xffff
        elif atom.type=='C':
            fields.update(Sb_bank=2,Sb_addr=0x100)
            extra['memory']=[dict(space='cbank',address=(2<<32)|0x100,
                                  hex=bval.to_bytes(4,'little').hex())]
        elif atom.type=='CX':
            cx_fields,cx_regs,memory=cx_source(klass,source,bval)
            fields.update(cx_fields);regs.update(cx_regs);extra['memory']=memory
        else:continue
        for is_min,flags in ((True,(1,0)),(False,(0,1))):
            fields['Pp@not']=int(not is_min)
            result=half_minmax(a,bval,is_min,False,False,False)
            add(klass.name,dict(fields),dict(regs),
                {'R8':result,'P2':flags[0],'P3':flags[1],'__native':True},**extra)
        # The patent identifies Pu/Pv as source indicators. Check the emitter's
        # source flags on one-NaN inputs and its canonical result on two NaNs.
        for av,bv,want,flags in (
                (0x7e007e00,0x7e007e00,0x7fff7fff,(1,1)),
                (0x7e007e00,0x3c003c00,0x3c003c00,(0,0)),
                (0x3c003c00,0x7e007e00,0x3c003c00,(1,1))):
            nan_fields=dict(fields);nan_regs=dict(regs);nan_extra=dict(extra)
            nan_regs['R2']=av
            if atom.type=='Register':nan_regs['R4']=bv
            elif atom.type=='UniformRegister':nan_regs['UR4']=bv
            elif atom.type=='F32Imm':nan_fields[source]=bv
            elif atom.type=='F64Imm':
                nan_fields[source]=(bv>>16)&0xffff;nan_fields[source+'1']=bv&0xffff
            elif atom.type=='C':
                nan_extra['memory']=[dict(space='cbank',address=(2<<32)|0x100,
                    hex=bv.to_bytes(4,'little').hex())]
            elif atom.type=='CX':
                nan_extra['memory']=[dict(space='ram',address=0x2000,
                    hex=bv.to_bytes(4,'little').hex())]
            for is_min in (True,False):
                nan_fields['Pp@not']=int(not is_min)
                add(klass.name,dict(nan_fields),dict(nan_regs),
                    {'R8':want,'P2':flags[0],'P3':flags[1],'__native':True},
                    **nan_extra)
        canonical_fields=dict(fields,nan='NAN')
        add(klass.name,canonical_fields,dict(regs),
            {'error':'sass_opaque_HMNMX2'},**extra)
    # HADD2.F32 adds two half lanes, takes the selected low result, and widens
    # it to F32. Its required first operand is -RZ; the selectors on the
    # second source choose which half supplies that low result.
    def widen_half(bits):
        kind,sign,value=softfloat.unpack(bits,16)
        if kind=='nan':return None
        if kind=='inf':return (sign<<31)|0x7f800000
        return softfloat.pack(value,32,zero_sign=sign)
    f32_values=[0x00000000,0x80000000,0x00010001,0x80010001,0x03ff0400,
                0x040003ff,0x35553aaa,0x3c004000,0xbc00c000,0x7bff3c00,
                0x7c003c00,0x3c007c00,0x7e003c00]
    for klass in (k for k in arch.classes if k.mnemonic=='HADD2' and
                  k.name.startswith('hadd2_F32') and not k.alternate):
        if any(atom.type=='CX' for atom in klass.operand_types.values()):
            bits,shift=(51,6) if sm=='SM90' else (45,4)
            base=0x1000;header=base//(1<<shift)+(1<<bits)
            loaded=0x3c003c00
            add(klass.name,dict(Rd=8,Ra='RZ',URc=4,Sc_offset=0,ofmt='F32',
                                **{'Ra@negate':1}),
                {'UR4':header&0xffffffff,'UR5':header>>32},
                {'R8':0x3f800000,'__native':True},
                memory=[dict(space='ram',address=base,
                             hex=loaded.to_bytes(4,'little').hex())])
            continue
        source=next(n for n in ('Rc','URc','Sc') if n in klass.operand_types)
        atom=klass.operand_types[source]
        swizzle_name=next((n for n in ('iswzB_as_C','iswzB') if n in klass.operand_types),None)
        for ftz_mode in ('noftz','FTZ'):
            for sat_mode in ('nosat','SAT'):
                for swizzle in (('H1_H0','H0_H0','H1_H1') if swizzle_name else ('H1_H0',)):
                    for absolute in (0,1):
                        for negate in (0,1):
                            for packed_value in f32_values:
                                fields={'Rd':8,'Ra':'RZ','ofmt':'F32','ftz':ftz_mode,'sat':sat_mode,
                                        'iswzA':'H1_H0','Ra@negate':1}
                                registers={};memory=[]
                                source_value=packed_value
                                if absolute:source_value &= 0x7fff7fff
                                if negate:source_value ^= 0x80008000
                                if atom.type=='Register':fields[source]=4;registers['R4']=packed_value
                                elif atom.type=='UniformRegister':fields[source]=4;registers['UR4']=packed_value
                                elif atom.type=='F16Imm':fields[source]=packed_value&0xffff;source_value=packed_value&0xffff
                                elif atom.type=='C':
                                    fields.update(Sc_bank=2,Sc_addr=0x100)
                                    memory=[dict(space='cbank',address=(2<<32)|0x100,
                                                 hex=packed_value.to_bytes(4,'little').hex())]
                                # Attribute modifiers live in the rule list, not
                                # operand_types; checking the latter silently
                                # left the encoded word unmodified while the
                                # reference applied abs/neg to source_value.
                                attrs={(r[1],r[2]) for _,r in klass.rules if r[0]=='attr'}
                                if (source,'absolute') in attrs:fields[source+'@absolute']=absolute
                                if (source,'negate') in attrs:fields[source+'@negate']=negate
                                if swizzle_name:fields[swizzle_name]=swizzle
                                lane=(source_value>>16)&0xffff if swizzle=='H1_H1' else source_value&0xffff
                                half=softfloat.arithmetic('add',[0x8000,lane],16,
                                    ftz=ftz_mode=='FTZ' and fp!='readable',sat=sat_mode=='SAT')
                                expected=widen_half(half)
                                if expected is None:continue
                                add(klass.name,fields,registers,{'R8':expected,'__native':True},**({'memory':memory} if memory else {}))
        # A 16-bit scalar literal occupies the low lane in this one-use form.
        if atom.type=='F16Imm':
            for half_value in (0x0001,0x3c00,0x8000,0xbc00):
                fields={'Rd':8,'Ra':'RZ','Sc':half_value,'ofmt':'F32','ftz':'noftz',
                        'sat':'nosat','iswzA':'H1_H0','Ra@negate':1}
                want=widen_half(softfloat.arithmetic('add',[0x8000,half_value],16))
                add(klass.name,fields,{}, {'R8':want,'__native':True})
    add('hfma2__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,ofmt='F32'),{},dict(error='sass_opaque_HFMA2'))
    if 'hfma2_mma__RRI_norelu' in classes:
        for bits in (0x3c004000,0x80000000,0x7e017d01,0x000103ff):
            canonical=lambda h:0x7fff if h&0x7c00==0x7c00 and h&0x3ff else h
            want=canonical(bits&0xffff)|(canonical(bits>>16)<<16)
            add('hfma2_mma__RRI_norelu',dict(Rd=8,Ra='RZ',Rb='RZ',Sc=bits>>16,Sc1=bits&0xffff,**{'Ra@negate':1}),{},dict(R8=want,__max_ops=1))
        add('hfma2_mma__RRI_norelu',dict(Rd=8,Ra=2,Rb='RZ',Sc=0x3c00,Sc1=0x4000,**{'Ra@negate':1}),{},dict(error='sass_opaque_HFMA2_MMA'))
    # Carry references use full unsigned sums, retaining negate's +1 and the
    # high product/addend. Overlapping predicate destinations capture inputs.
    mask=0xffffffff;mask64=(1<<64)-1
    carry_vectors=[(0,0,0),(0xffffffff,0xffffffff,0xffffffff),(0x80000000,0x80000000,0),
                   (0xffffffff,1,0),(0,0xffffffff,1)]
    rng=random.Random('carry')
    carry_vectors += [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(12)]
    from itertools import product
    for a,b,c in carry_vectors:
        for ext in (False,True):
            for flags in (0,1,2,4,5,6):
                xs=[a,b,c]
                attrs={n+'@'+('invert' if ext else 'negate'):flags>>i&1 for i,n in enumerate(('Ra','Rb','Rc'))}
                for i in range(3):
                    if flags>>i&1:xs[i]=mask-xs[i]+(0 if ext else 1)
                for p,q in (product((0,1),repeat=2) if ext else [(0,0)]):
                    total=sum(xs)+p+q
                    cls='iadd3_x_noimm__RRR_RRR' if ext else 'iadd3_noimm__RRR_RRR'
                    fields=dict(Rd=2,Pu=0,Pv=1,Ra=2,Rb=4,Rc=6,**attrs)
                    if ext:fields.update(Pp=0,Pq=1)
                    add(cls,fields,dict(R2=a,R4=b,R6=c,P0=p,P1=q),dict(R2=total&mask,P0=int(total>mask),P1=int(total>0x1ffffffff),__native=True))
        total=a+b+c+1
        add('uiadd3_x__URURUR_URURUR',dict(URd=2,UPu=0,UPv=1,URa=2,URb=4,URc=6,UPp=0,UPq=1),
            dict(UR2=a,UR4=b,UR6=c,UP0=1,UP1=0),dict(UR2=total&mask,UP0=int(total>mask),UP1=int(total>0x1ffffffff),__native=True))
        total=((((c<<32)|a)<<5)>>32&mask)+b+1
        add('ulea_hi_noimm_x__URURUR_URURUR',dict(URd=2,UPu=0,URa=2,URb=4,URc=6,scaleU5=5,UPp=0),
            dict(UR2=a,UR4=b,UR6=c,UP0=1),dict(UR2=total&mask,UP0=int(total>mask),__native=True))
        total=a*b+((b<<32)|c)+1
        add('uimad_wide_x__URURUR_URURUR',dict(URd=2,UPu=0,URa=2,URb=4,URc=6,UPp=0,fmt='U32'),
            dict(UR2=a,UR4=b,UR6=c,UR7=b,UP0=1),dict(UR2=total&mask,UR3=total>>32&mask,UP0=int(total>mask64),__native=True))
        for ext in (False,True):
            cls='iadd3_x_noimm__RRR_RRR' if ext else 'iadd3_noimm__RRR_RRR'
            fields=dict(Rd=2,Pu=0,Pv=0,Ra=2,Rb=4,Rc=6)
            if ext:fields.update(Pp=0,Pq=1)
            total=a+b+c+(1 if ext else 0)
            add(cls,fields,dict(R2=a,R4=b,R6=c,P0=1,P1=0),dict(R2=total&mask,P0=int(total>mask),__native=True))
        for cls in ('lea_lo_noimm__RRR_RRR','lea_lo_noimm_x__RRR_RRR','lea_hi_noimm__RRR_RRR',
                    'lea_hi_noimm_x__RRR_RRR','lea_hi_noimm_sx32__RRR_RRR','lea_hi_noimm_sx32_x__RRR_RRR'):
            ext='_x__' in cls;hi='_hi_' in cls;sx='_sx32' in cls
            for shift in (0,1,5,31):
                for flags in (0,1,2):
                    source=((a-(1<<32) if a>>31 else a) if sx else (c<<32)|a) if hi else a
                    word=((source<<shift)>>(32 if hi else 0))&mask
                    if flags&1:word=mask-word+(0 if ext else 1)
                    bv=mask-b+(0 if ext else 1) if flags&2 else b
                    for p in ((0,1) if ext else (0,)):
                        total=word+bv+p
                        fields=dict(Rd=2,Pu=0,Ra=2,Rb=4,scaleU5=shift,
                                    **{'Ra@'+('invert' if ext else 'negate'):flags&1,'Rb@'+('invert' if ext else 'negate'):flags>>1})
                        if hi and not sx:fields['Rc']=6
                        if ext:fields['Pp']=0
                        add(cls,fields,dict(R2=a,R4=b,R6=c,P0=p),dict(R2=total&mask,P0=int(total>mask),__native=True))
        for mode in ('LO','HI','WIDE'):
            for ext in (False,True):
                if mode=='LO' and not ext:continue
                cls=('imad'+('_'+mode.lower() if mode!='LO' else '')+('_x' if ext else '')+'__RRR_RRR')
                for fmt in ('U32','S32'):
                    av=a-(1<<32) if fmt=='S32' and a>>31 else a
                    bv=b-(1<<32) if fmt=='S32' and b>>31 else b
                    prod=(av*bv)&mask64
                    for invert in ((0,1) if ext else (0,)):
                        addend=(b<<32)|c if mode!='LO' else c
                        if invert:addend^=mask64 if mode!='LO' else mask
                        for p in ((0,1) if ext else (0,)):
                            total=prod+addend+p
                            fields=dict(Rd=8,Ra=2,Rb=4,Rc=6,fmt=fmt)
                            if mode!='LO':fields['Pu']=0
                            if ext:fields.update(Pp=0,**{'Rc@invert':invert})
                            expected=dict(R8=(total>>(32 if mode=='HI' else 0))&mask,__native=True)
                            if mode=='WIDE':expected['R9']=total>>32&mask
                            if mode!='LO':expected['P0']=int(total>mask64)
                            add(cls,fields,dict(R2=a,R4=b,R6=c,R7=b,P0=p),expected)
    # Exhaust the selector domains changed by decode-time specialization. The
    # references calculate results from bit truth tables and integer arithmetic.
    x,y,z=0x81234567,0x76543210,0xa5a5f0f0
    for lut in range(256):
        result=sum(((lut >> (((x>>i&1)<<2)|((y>>i&1)<<1)|(z>>i&1)))&1)<<i for i in range(32))
        add('lop3_lut__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6,imm8=lut,Pp='PT'),
            dict(R2=x,R4=y,R6=z),dict(R2=result))
    for fmt in ('U32','S32','U64','S64'):
        for direction in ('L','R'):
            for mode in ('C','W'):
                for half in ('LO','HI'):
                    for shift in (0,31,32,63,64,65):
                        width=32 if fmt.endswith('32') else 64
                        count=min(shift,width) if mode=='C' else shift&(width-1)
                        value=(z<<32)|x
                        if direction=='R' and fmt.startswith('S') and value>>63:value-=1<<64
                        value=(value<<count if direction=='L' else value>>count)&0xffffffffffffffff
                        result=(value>>(32 if half=='HI' else 0))&0xffffffff
                        add('shf__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6,fmt=fmt,dir=direction,cw=mode,hilo=half),
                            dict(R2=x,R4=shift,R6=z),dict(R2=result))
    for fmt in ('U32','S32'):
        left=x if fmt=='U32' else x-(1<<32);right=y
        comparisons={'F':False,'LT':left<right,'EQ':left==right,'LE':left<=right,
                     'GT':left>right,'NE':left!=right,'GE':left>=right,'T':True}
        for operation,condition in comparisons.items():
            for bop in ('AND','OR','XOR'):
                for predicate in (0,1):
                    def combine(v):
                        return int((v and predicate) if bop=='AND' else (v or predicate) if bop=='OR' else bool(v)^bool(predicate))
                    add('isetp__RRR_RRR_noEX',dict(Pu=0,Pv=1,Ra=2,Rb=4,Pp=0,icmp=operation,bop=bop,fmt=fmt),
                        dict(R2=x,R4=y,P0=predicate),dict(P0=combine(condition),P1=combine(not condition)))
    # CuLifter's ISETP.EX consumes Pr as the low-word comparison result and
    # compares the encoded operands as high words. Exercise every comparison
    # and predicate combiner across the direct source forms.
    ex_pairs=[(0,0,0),(0x80000000,0x7fffffff,1),
              (0x7fffffff,0x80000000,0),(0xffffffff,0,1)]
    for klass in (k for k in arch.classes if k.mnemonic=='ISETP' and
                  'ex' in k.operand_types and not k.alternate):
        src=next((n for n in ('Rb','URb','Sb') if n in klass.operand_types),None)
        if src is None or any(atom.type=='CX' for atom in klass.operand_types.values()):continue
        atom=klass.operand_types[src]
        ex_en=arch.enums[klass.operand_types['ex'].type]
        ex_modes=['noex','EX'] if 'noex' in ex_en else ['EX']
        for fmt in ('U32','S32'):
            if fmt not in arch.enums[klass.operand_types['fmt'].type]:continue
            for ex_mode in ex_modes:
              for av,bv,low in ex_pairs:
                for operation in ('F','LT','EQ','LE','GT','NE','GE','T'):
                    sa=av if fmt=='U32' or av<0x80000000 else av-(1<<32)
                    sb=bv if fmt=='U32' or bv<0x80000000 else bv-(1<<32)
                    compare={'F':False,'LT':sa<sb,'EQ':sa==sb,'LE':sa<=sb,
                             'GT':sa>sb,'NE':sa!=sb,'GE':sa>=sb,'T':True}[operation]
                    for bop in ('AND','OR','XOR'):
                        for predicate,pr_not,p_not in ((0,0,0),(1,0,0),(0,1,0),(1,1,1)):
                            low_value=low ^ pr_not
                            cond=(low_value if av==bv else compare) if ex_mode=='EX' else compare
                            p_value=predicate ^ p_not
                            def combine_ex(v):
                                return int((bool(v) and bool(p_value)) if bop=='AND' else
                                           (bool(v) or bool(p_value)) if bop=='OR' else
                                           bool(v)^bool(p_value))
                            fields={'Pu':0,'Pv':1,'Ra':2,'Pp':4,'Pr':5,
                                    'icmp':operation,'fmt':fmt,'bop':bop,
                                    'ex':ex_mode,'Pp@not':p_not,'Pr@not':pr_not}
                            regs={'R2':av,'P4':predicate,'P5':low}
                            memory=[]
                            if atom.type=='Register':fields[src]=6;regs['R6']=bv
                            elif atom.type=='UniformRegister':fields[src]=6;regs['UR6']=bv
                            elif atom.type=='C':
                                fields.update(Sb_bank=2,Sb_addr=16)
                                memory=[dict(space='cbank',address=(2<<32)|16,
                                             hex=bv.to_bytes(4,'little').hex())]
                            else:fields[src]=bv
                            add(klass.name,fields,regs,
                                {'P0':combine_ex(cond),'P1':combine_ex(not cond),'__native':True},
                                memory=memory)
    for name,cls,fields,message in [('loop_limit','bra_',dict(sImm=-16),'kernel instruction limit'),
                                    ('zero_target','bra_',dict(sImm=-0x100010),'terminated without EXIT'),
                                    ('fallthrough','nop_',{},'PC outside kernel')]:
        if cls not in available:continue
        word=encode(sm,cls,**fields)
        request=dict(kernel=dict(name=name,code=word,threads=1,max_steps=4),output=dict(address=0x200000000))
        out.append((name,request,dict(error=message)))
    if fp=='readable':
        out=[c for c in out if c[0] not in classes or classes[c[0]].mnemonic not in READABLE_FLOATS]
        readable_cases(sm,add,classes,rng)
        # The generic readable fixture generator uses direct registers and
        # does not cover composite CX operands. Restore focused CX cases after
        # filtering the exact packed-half fixtures above.
        for klass in (k for k in arch.classes if k.mnemonic=='HADD2' and
                      k.name=='hadd2__RCx' and not k.alternate):
            cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
            fields={'Rd':8,'Ra':2}
            cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
            add(klass.name,fields,{'R2':0x3c003c00,**cx_regs},
                {'R8':0x42004200,'__native':True},memory=memory)
        for klass in (k for k in arch.classes if k.mnemonic=='HFMA2' and
                      k.name=='hfma2__RCxR' and not k.alternate):
            cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
            fields={'Rd':8,'Ra':2,'Rc':6}
            cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
            add(klass.name,fields,{'R2':0x3c003c00,'R6':0x42004200,**cx_regs},
                {'R8':0x45004500,'__native':True},memory=memory)
        for klass in (k for k in arch.classes if k.mnemonic=='HMUL2' and
                      k.name=='hmul2__RCx' and not k.alternate):
            cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
            fields={'Rd':8,'Ra':2}
            cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x40004000);fields.update(cx_fields)
            add(klass.name,fields,{'R2':0x3c003c00,**cx_regs},
                {'R8':0x40004000,'__native':True},memory=memory)
        for klass in (k for k in arch.classes if k.mnemonic=='HADD2' and
                      k.name.startswith('hadd2_F32') and not k.alternate and
                      any(a.type=='CX' for a in k.operand_types.values())):
            cx_name=next(n for n,a in klass.operand_types.items() if a.type=='CX')
            fields={'Rd':8,'Ra':'RZ','ofmt':'F32','Ra@negate':1}
            cx_fields,cx_regs,memory=cx_source(klass,cx_name,0x3c003c00);fields.update(cx_fields)
            add(klass.name,fields,cx_regs,{'R8':0x3f800000,'__native':True},memory=memory)
    return out


# Classes whose readable lowering differs from the exact one.
READABLE_FLOATS={'FADD','FMUL','FFMA','DADD','DMUL','DFMA','HADD2','HFMA2','HMUL2','FSEL','FMNMX','FSETP'}

def readable_reference(op,inputs,width,sat=False,scale=0):
    """Unfused round-to-nearest formula without flushing; None where a NaN or
    Ghidra's minimum-subnormal underflow band makes the comparison meaningless."""
    from fractions import Fraction
    def step(op,xs,sat):
        parts=[softfloat.unpack(x,width) for x in xs]
        if all(k in ('zero','finite') for k,_,_ in parts):
            v=abs(parts[0][2]*parts[1][2] if op=='mul' else parts[0][2]+parts[1][2])
            tiny=Fraction(2)**(1-(1<<(softfloat.FORMATS[width][0]-1))+1-softfloat.FORMATS[width][1])
            if tiny/2<v<tiny:return None
        return softfloat.arithmetic(op,xs,width,sat=sat)
    two={32:0x3f800000,64:0x3ff0000000000000}
    if op=='fma':steps=[('mul',inputs[:2]),('add',[None,inputs[2]])]
    else:steps=[(op,inputs)]
    if scale:steps.append(('mul',[None,two[width]+(scale<<softfloat.FORMATS[width][1])]))
    value=None
    for i,(o,xs) in enumerate(steps):
        value=step(o,[value if x is None else x for x in xs],sat and i==len(steps)-1)
        if value is None or softfloat.unpack(value,width)[0]=='nan':return None
    return value

def readable_cases(sm,add,classes,rng):
    import struct,random
    edges=[0,0x80000000,1,0x80000001,0x007fffff,0x00800000,0x3f800000,0xbf800000,0x7f7fffff,0x7f800000,0x3effffff,0x4b800001]
    rng=random.Random('readable_fp32')
    floats=edges+[rng.getrandbits(32)&0xbfffffff for _ in range(10)]+[rng.getrandbits(32) for _ in range(6)]
    vectors=[(floats[i],floats[(i*7+3)%len(floats)],floats[(i*5+1)%len(floats)]) for i in range(len(floats))]
    for i,(x,y,z) in enumerate(vectors):
        rnd=('RN','RZ','RM','RP')[i%4]
        for flush in (False,True):
            for sat in (False,True):
                extra=dict(rnd=rnd,sat='SAT' if sat else 'nosat')
                for op,cls,fields,inputs in [('add','fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,**({'ftz':'FTZ'} if flush else {})),[x,y]),
                                             ('mul','fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4,**({'fmz':'FMZ'} if flush else {})),[x,y]),
                                             ('fma','ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,**({'fmz':'FTZ'} if flush else {})),[x,y,z])]:
                    want=readable_reference(op,inputs,32,sat)
                    if want is not None:add(cls,dict(fields,**extra),dict(R2=x,R4=y,R6=z),dict(R8=want,__native=True))
        for scale,k in [('D2',-1),('M8',3)]:
            want=readable_reference('mul',[x,y],32,scale=k)
            if want is not None:add('fmul__RRR_RR',dict(Rd=8,Ra=2,Rb=4,scale=scale,rnd=rnd),dict(R2=x,R4=y),dict(R8=want,__native=True))
    # The plain formula is one operation per arithmetic operator, plus the result copy.
    add('ffma__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,rnd='RZ'),dict(R2=0x3fc00000,R4=0x40000000,R6=0xbf800000),dict(R8=0x40000000,__max_ops=3))
    add('fadd__RRR_RR',dict(Rd=8,Ra=2,Rc=4,ftz='FTZ'),dict(R2=0x3f800000,R4=0x40000000),dict(R8=0x40400000,__max_ops=2))
    def pair(name,bits):return {name:bits&0xffffffff,f'R{int(name[1:])+1}':bits>>32}
    rng=random.Random('readable_fp64')
    values64=[0,1<<63,1,0x0010000000000000,0x3ff0000000000000,0xbff0000000000000,0x7fefffffffffffff,
              0x3ff0000000000001]+[rng.getrandbits(64) for _ in range(16)]
    for i,x in enumerate(values64):
        y,z=values64[(i*7+3)%len(values64)],values64[(i*5+1)%len(values64)]
        for op,cls,fields,inputs in [('add','dadd__RRR_RR',dict(Rd=2,Ra=2,Rc=4),[x,y]),
                                     ('mul','dmul__RRR_RR',dict(Rd=2,Ra=2,Rb=4),[x,y]),
                                     ('fma','dfma__RRR_RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6),[x,y,z])]:
            want=readable_reference(op,inputs,64)
            if want is not None:
                add(cls,dict(fields,rnd=('RN','RZ','RM','RP')[i%4]),{**pair('R2',x),**pair('R4',y),**pair('R6',z)},dict(pair('R2',want),__native=True))
    rng=random.Random('readable_fp16')
    packed=[0x3c004000,0x00010001,0x7bfffbff,0xbc00c000]+[rng.getrandbits(32) for _ in range(10)]
    for i,x in enumerate(packed):
        y,z=packed[(i*7+3)%len(packed)],packed[(i*5+1)%len(packed)]
        for op,cls,fields,inputs in [('add','hadd2__RR',dict(Rd=2,Ra=2,Rc=4,ftz='FTZ'),[x,y]),
                                     ('mul','hmul2__RR',dict(Rd=2,Ra=2,Rb=4),[x,y]),
                                     ('fma','hfma2__RRR',dict(Rd=2,Ra=2,Rb=4,Rc=6,fmz='FMZ'),[x,y,z])]:
            lanes=[readable_reference(op,[v>>(16*lane)&0xffff for v in inputs],16) for lane in (0,1)]
            if None not in lanes:add(cls,fields,dict(R2=x,R4=y,R6=z),dict(R2=lanes[0]|lanes[1]<<16,__native=True))
    def f32(v):return struct.unpack('<f',v.to_bytes(4,'little'))[0]
    for a,c in [(1,0x80000001),(0x3f800000,0x40000000),(0x40000000,0x3f800000),(0,0x80000000),(0x007fffff,0x00000001)]:
        for pred in (0,1):
            fields=dict(Rd=2,Ra=2,Rb=4,Pp=0,ftz='FTZ')
            add('fsel__RRR_RRR',fields,dict(R2=a,R4=c,P0=pred),dict(R2=a if pred else c,__native=True))
            nan={'nan':'NAN'} if 'nan' in classes['fmnmx__RRR_RRR'].operand_types else {}
            add('fmnmx__RRR_RRR',dict(fields,**nan),dict(R2=a,R4=c,P0=pred),dict(R2=a if (f32(a)<f32(c))==bool(pred) else c,__native=True))
        add('fsetp__RRR_RRR',dict(Pu=0,Pv=1,Pp='PT',Ra=2,Rb=4,fcomp='LT',bop='AND',ftz='FTZ'),
            dict(R2=a,R4=c),dict(P0=int(f32(a)<f32(c)),P1=int(not f32(a)<f32(c)),__native=True))


def run(sm='SM89',fp='exact',todo=None):
    from sass.gen_sleigh import variant
    if todo is None:todo=cases(sm,fp)
    inp=''.join(json.dumps(request)+'\n' for _,request,_ in todo)
    process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),f'SASS:LE:64:{variant(sm,fp)}'],input=inp,capture_output=True,text=True)
    results=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    if len(results)!=len(todo):raise RuntimeError(process.stderr[-2500:])
    failures=[]
    for (cls,request,expected),result in zip(todo,results):
        if 'error' in expected:
            if expected['error'] not in result.get('error',''):failures.append((cls,expected,result))
        else:
            registers={n:v for n,v in expected.items() if not n.startswith('__')}
            ok=result.get('registers')==registers
            if '__counter' in expected:ok=ok and result.get('counter')==expected['__counter']
            if '__memory' in expected:ok=ok and bool(result.get('memory')) and result['memory'][0]['hex']==expected['__memory']
            if '__events' in expected:ok=ok and len(result.get('events',[]))==expected['__events']
            if '__native' in expected:
                ops=result.get('pcode_ops',[])
                ok=ok and bool(ops) and 'CALLOTHER' not in ops
            if '__max_ops' in expected:
                ops=result.get('pcode_ops',[])
                ok=ok and bool(ops) and len(ops)<=expected['__max_ops'] and not set(ops)&{'CBRANCH','CALLOTHER'}
            if not ok:failures.append((cls,expected,result))
    print(f'{variant(sm,fp)}: semantics {len(todo)-len(failures)}/{len(todo)}')
    for failure in failures[:20]:print(failure)
    assert not failures

def test_semantics():
    import pytest
    if os.environ.get('SASS_SEMANTICS')!='1':pytest.skip('set SASS_SEMANTICS=1 for Ghidra integration')
    for fp in ('exact','readable'):run(os.environ.get('SASS_TEST_ARCH','SM89'),fp)


def test_semantics_gpu():
    import pytest
    if os.environ.get('SASS_GPU')!='1':pytest.skip('set SASS_GPU=1 for the SM89 hardware oracle')
    from gpu_semantics import Driver
    try:driver=Driver()
    except (OSError,RuntimeError) as error:pytest.skip(str(error))
    driver.close()
    subprocess.run([sys.executable,str(ROOT/'tests/gpu_semantics.py'),'--require-gpu'],check=True)
    subprocess.run([sys.executable,str(ROOT/'tests/carry_semantics.py'),'--require-gpu'],check=True)
    subprocess.run([sys.executable,str(ROOT/'tests/bmsk_semantics.py'),'--require-gpu'],check=True)
    subprocess.run([sys.executable,str(ROOT/'tests/bmsk_cx_semantics.py'),'--require-gpu'],check=True)


if __name__=='__main__':run(sys.argv[1] if len(sys.argv)>1 else 'SM89',sys.argv[2] if len(sys.argv)>2 else 'exact')
