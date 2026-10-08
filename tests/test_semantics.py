"""Execute actual generated p-code against independent scalar references.

Usage: SASS_SLEIGH_OUT=... python3 tests/test_semantics.py SM89 [exact|readable]
"""
import json,os,random,subprocess,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from semantic_cases import encode
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT
from sass import ir,softfloat


def cases(sm,fp='exact'):
    arch=ir.load(sm)
    classes={k.name:k for k in arch.classes if not k.alternate}
    available=set(classes);out=[]
    def add(cls,fields,registers,expected,**extras):
        if cls not in available:return
        if sm in ('SM75','SM80') and cls in ('imad_wide__RRC_RRC','imad_hi__RRC_RRC'):
            expected=dict(error='sass_opaque_IMAD')
        if sm=='SM120' and cls=='imnmx__RRR_RRR':
            # This architecture adds a second predicate input and two predicate
            # results. Its convention is intentionally an explicit fallback.
            expected=dict(error='sass_opaque_IMNMX')
        request=dict(word=encode(sm,cls,**fields),registers=registers,observe=[n for n in expected if not n.startswith("__")],**extras)
        if '__max_ops' in expected or '__native' in expected:request['inspect_pcode']=True
        out.append((cls,request,expected))
    rng=random.Random(7)
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
    add('fmnmx_pred__RRR_RRR',dict(Rd=8,Pu=0,Ra=2,Rb=4,Pp='PT'),{},dict(error='sass_opaque_FMNMX'))
    if 'nan' in classes['fmnmx__RRR_RRR'].operand_types:
        add('fmnmx__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,Pp='PT',nan='NAN'),{},dict(error='sass_opaque_FMNMX'))
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
    add('imad_wide__RRC_RRC',dict(Rd=8,Ra=2,Rb=4,Sc_bank=2,Sc_addr=16,fmt='U32'),dict(R2=3,R4=5),dict(R8=22,R9=2),memory=[dict(space='cbank',address=(2<<32)|16,hex='0700000002000000')])
    add('imad_hi__RRC_RRC',dict(Rd=8,Ra=2,Rb=4,Sc_bank=2,Sc_addr=16,fmt='U32'),dict(R2=3,R4=5),dict(R8=2),memory=[dict(space='cbank',address=(2<<32)|16,hex='0700000002000000')])
    add('mov__RC',dict(Rd=8,Sb_bank=2,Sb_addr=16,PixMaskU04=15),{},dict(R8=0x12345678),memory=[dict(space='cbank',address=(2<<32)|16,hex='78563412')])
    for label,data,want in [('U8','ff',255),('S8','ff',0xffffffff),('U16','ffff',65535),
                            ('S16','ffff',0xffffffff),('32','78563412',0x12345678),
                            ('64','0100000002000000',1)]:
        expected=dict(UR8=want)
        if label=='64':expected['UR9']=2
        add('uldc_const__RCR',dict(URd=8,Sa_bank=2,Sa_addr=16,sz=label),{},expected,
            memory=[dict(space='cbank',address=(2<<32)|16,hex=data)])
    add('i2fp__RRR',dict(Rd=8,Rb=2,srcfmt='S32',dstfmt='F32'),dict(R2=0xfffffffe),dict(R8=0xc0000000))
    if sm=='SM89':
        for bits,want in [(0x7fc00000,0),(0x7f800000,0x7fffffff),(0xff800000,0x80000000),
                          (0x4f000000,0x7fffffff),(0xcf000001,0x80000000),(0x3fc00000,1)]:
            add('f2i__Rb_32b',dict(Rd=8,Rb=2,dstfmt='S32',rnd='TRUNC',ntz='NTZ'),
                dict(R2=bits),dict(R8=want))
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
    add('hadd2__RR',dict(Rd=8,Ra=2,Rc=4,ftz='FTZ'),{},dict(error='sass_opaque_HADD2'))
    add('hfma2__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,fmz='FMZ'),{},dict(error='sass_opaque_HFMA2'))
    for cls,fields in [('hadd2_F32__RR',dict(Rd=8,Ra=2,Rc=4)),
                       ('hfma2__RRR',dict(Rd=8,Ra=2,Rb=4,Rc=6,ofmt='F32'))]:
        add(cls,fields,{},dict(error='sass_opaque_'+('HADD2' if cls.startswith('hadd') else 'HFMA2')))
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
    return out


# Classes whose readable lowering differs from the exact one.
READABLE_FLOATS={'FADD','FMUL','FFMA','DADD','DMUL','DFMA','HADD2','HFMA2','FSEL','FMNMX','FSETP'}

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
    import struct
    edges=[0,0x80000000,1,0x80000001,0x007fffff,0x00800000,0x3f800000,0xbf800000,0x7f7fffff,0x7f800000,0x3effffff,0x4b800001]
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
    packed=[0x3c004000,0x00010001,0x7bfffbff,0xbc00c000]+[rng.getrandbits(32) for _ in range(10)]
    for i,x in enumerate(packed):
        y,z=packed[(i*7+3)%len(packed)],packed[(i*5+1)%len(packed)]
        for op,cls,fields,inputs in [('add','hadd2__RR',dict(Rd=2,Ra=2,Rc=4,ftz='FTZ'),[x,y]),
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


def run(sm='SM89',fp='exact'):
    from sass.gen_sleigh import variant
    todo=cases(sm,fp)
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


if __name__=='__main__':run(sys.argv[1] if len(sys.argv)>1 else 'SM89',sys.argv[2] if len(sys.argv)>2 else 'exact')
