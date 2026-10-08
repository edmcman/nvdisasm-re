"""SM89 whole-kernel CUDA/Ghidra semantics oracle.

The GPU and PcodeEmulator execute the same cubin text, including its prologue,
address calculations, memory operations, and EXIT. Threads have independent
emulator states; warp communication and cross-thread shared memory are excluded.
"""
import argparse,ctypes as C,json,os,random,re,struct,subprocess,sys,tempfile
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from sass.cubin import kernel_image
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT

SPECS={
 'iadd3':('IADD3',), 'imad':('IMAD',), 'imad_hi':('IMAD.HI',),
 'lop3':('LOP3',), 'shf':('SHF',), 'iabs':('IABS',), 'imnmx':('IMNMX',),
 'dadd':('DADD',), 'dmul':('DMUL',), 'dfma':('DFMA',), 'dsetp':('DSETP',),
 'hadd2':('HADD2',), 'hfma2':('HFMA2',), 'hadd2_sat':('HADD2.SAT',),
 'hfma2_sat':('HFMA2.SAT',), 'hfma2_move':('HFMA2.MMA',),
 'hfma2_move_zero':('HFMA2.MMA',), 'hfma2_move_nan':('HFMA2.MMA',),
 'hfma2_move_denorm':('HFMA2.MMA',), 'hfma2_move_inf':('HFMA2.MMA',),
 'hadd2_lanes':('HADD2',), 'hfma2_lanes':('HFMA2',),
 'prmt':('PRMT',), 'fadd':('FADD',), 'fmul':('FMUL',), 'ffma':('FFMA',),
 'isetp':('ISETP',), 'sel':('ISETP',), 'fsetp':('FSETP',), 'f2i':('F2I',),
 'fsel':('FSEL',), 'fmin':('FMNMX',), 'fmax':('FMNMX',),
 'fmin_ftz':('FMNMX.FTZ',), 'fmax_ftz':('FMNMX.FTZ',),
 'i2f':('I2FP',), 'fadd_ftz':('FADD.FTZ',), 'fmul_rz':('FMUL.RZ',),
 'ffma_sat':('FFMA.SAT',), 'shared':('LDS','STS'), 'local':('LDL','STL'),
 'branch':('BRA','BSSY','BSYNC'), 'ldc':('LDC',)}
HALF_FLOAT_OUTPUTS={'hadd2','hfma2','hadd2_sat','hfma2_sat','hadd2_lanes','hfma2_lanes'}
DOUBLE_FAMILIES={'dadd','dmul','dfma','dsetp'}
FLOAT_OUTPUTS={'dadd','dmul','dfma','fadd','fmul','ffma','i2f','fadd_ftz','fmul_rz','ffma_sat'}



def kernel_request(cubin, family, data):
    image=kernel_image(cubin,'sem_'+family)
    if image['arch']!='SM89':raise ValueError('SM89 cubin required')
    if [p['size'] for p in image['parameters']] != [8,8]:
        raise ValueError('oracle kernels require two pointer parameters')
    size=8 if family in DOUBLE_FAMILIES else 4
    count=len(data)//(3*size)
    if not count or len(data)%(3*size) or count%32:raise ValueError('input must contain whole 32-thread blocks')
    input_address,output_address=0x100000000,0x200000000
    constants=dict(image['constants']);bank=bytearray(constants[0])
    # CUDA launch dimensions and local stack base, before the metadata-defined
    # parameter region. These offsets are confirmed from SM89 compiler output.
    struct.pack_into('<III',bank,0,32,1,1)
    struct.pack_into('<I',bank,0x28,0x1000)
    for parameter,value in zip(image['parameters'],(input_address,output_address)):
        struct.pack_into('<Q',bank,image['parameter_base']+parameter['offset'],value)
    constants[0]=bytes(bank)
    memory=[dict(space='cbank',address=index<<32,hex=value.hex()) for index,value in constants.items()]
    memory += [dict(space='ram',address=input_address,hex=data.hex()),
               dict(space='ram',address=output_address,hex=('cc'*count*size))]
    return dict(kernel=dict(name=image['name'],code=image['code'].hex(),threads=count,block_size=32),
                memory=memory,output=dict(address=output_address,size=size,stride=size),
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
        'hfma2_lanes':('HFMA2',None,dict(iswzA='H1_H1',iswzB='H0_H0',iswzC='H1_H0',**{'Ra@negate':1,'Rb@absolute':1}))}
    for suffix,bits in [('zero',0x80000000),('nan',0xfe01fd01),('denorm',0x000103ff),('inf',0x7c00fc00)]:
        changes['hfma2_move_'+suffix]=('HFMA2','hfma2_mma__RRI_norelu',
            dict(Ra=255,Rb=255,Sc=bits>>16,Sc1=bits&0xffff,**{'Ra@negate':1,'Rb@negate':0}))
    for family,(mnemonic,classname,fields) in changes.items():
        section='.text.sem_'+family;code=image[section];matches=[]
        for offset in range(0,len(code),16):
            decoded=pydecode.decode('SM89',code[offset:offset+16])
            if decoded.klass.mnemonic==mnemonic:matches.append((offset,decoded))
        if len(matches)!=1:raise ValueError(f'{family}: expected one {mnemonic}, got {len(matches)}')
        offset,decoded=matches[0]
        target=next(k for k in ir.load('SM89').classes if k.name==(classname or decoded.klass.name))
        names=target.operand_types
        values={n:v for n,v in decoded.env.items() if isinstance(n,str) and n in names}
        values.update({('@'.join(n)):v for n,v in decoded.env.items() if isinstance(n,tuple) and len(n)==2 and n[0] in names})
        values.update(fields)
        replacement=int.from_bytes(bytes.fromhex(encode('SM89',classname or decoded.klass.name,**values)),'little')
        old=int.from_bytes(code[offset:offset+16],'little');mask=(1<<105)-1
        data[offsets[section]+offset:offsets[section]+offset+16]=((replacement&mask)|(old&~mask)).to_bytes(16,'little')
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
        size=8 if family in DOUBLE_FAMILIES else 4
        a=self.malloc(len(data));n=len(data)//(3*size);b=self.malloc(n*size)
        inp=C.create_string_buffer(data);result=C.create_string_buffer(n*size)
        self.call('cuMemcpyHtoD_v2',a,inp,C.c_size_t(len(data)))
        params=(C.c_void_p*2)(C.cast(C.byref(a),C.c_void_p),C.cast(C.byref(b),C.c_void_p))
        self.call('cuLaunchKernel',fn,C.c_uint(n//32),C.c_uint(1),C.c_uint(1),C.c_uint(32),C.c_uint(1),C.c_uint(1),C.c_uint(0),C.c_void_p(),params,C.c_void_p())
        self.call('cuCtxSynchronize');self.call('cuMemcpyDtoH_v2',result,b,C.c_size_t(n*size))
        self.call('cuModuleUnload',self.module);self.module=C.c_void_p()
        return struct.unpack('<'+('Q' if size==8 else 'I')*n,result.raw)

def main():
    p=argparse.ArgumentParser();p.add_argument('--compile-only',action='store_true');p.add_argument('--require-gpu',action='store_true');p.add_argument('--output',default='/tmp/sass-gpu-oracle');a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True);cubin=out/'scalar_semantics.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler','-diag-suppress=177','-cubin','-arch=sm_89',str(ROOT/'tests/kernels/scalar_semantics.cu'),'-o',str(cubin)],check=True)
    patched=patch_half_fixtures(cubin)
    sass=subprocess.check_output([os.environ.get('CUOBJDUMP','/usr/local/cuda/bin/cuobjdump'),'-sass',str(cubin)],text=True);(out/'scalar_semantics.sass').write_text(sass)
    for family,mnemonics in SPECS.items():
        match=re.search(r'Function : sem_'+family+r'\b(.*?)(?=Function :|\Z)',sass,re.S)
        for mnemonic in mnemonics:
            if not match or not re.search(r'\b'+re.escape(mnemonic)+r'(?:\.|\s)',match[1]):raise RuntimeError('fixture does not contain '+mnemonic+' for '+family)
    print(f'Compiled and confirmed {len(SPECS)} SM89 kernel fixtures',flush=True)
    if a.compile_only:return
    try:driver=Driver()
    except (OSError,RuntimeError) as e:
        if a.require_gpu:raise
        print('SKIP GPU execution: '+str(e));return
    try:
        rng=random.Random(1);fixtures=[];requests=[];results=[]
        for family in SPECS:
            vectors=[[rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
            edges=[0,0xffffffff,0x80000000,0x7fffffff,0x7f800000,0xff800000,0x7fc00000,1,
                   0x007fffff,0x00800000,0x3f800000,0xbf800000,0x7f7fffff,0x80000001]
            vectors[:len(edges)]=[[x,x,x] for x in edges]
            if family in ('fsel','fmin','fmax','fmin_ftz','fmax_ftz'):
                pairs=[(0,0x80000000),(0x80000000,0),(0x7fc00000,0x3f800000),
                       (0x3f800000,0x7fc00000),(0x7f800001,0xff800001),
                       (1,0),(0x80000001,0),(0x007fffff,0x80000001)]
                vectors[len(edges):len(edges)+2*len(pairs)]=[[x,y,p] for x,y in pairs for p in (0,1)]
            if family in HALF_FLOAT_OUTPUTS or family=='hfma2_move':
                edges16=[0,0x8000,1,0x8001,0x03ff,0x0400,0x3c00,0xbc00,0x7bff,0xfbff,0x7c00,0xfc00,0x7e01]
                vectors[:len(edges16)]=[[x|(edges16[(i+3)%len(edges16)]<<16),
                    edges16[(i+5)%len(edges16)]|(x<<16),
                    edges16[(i+7)%len(edges16)]|(edges16[(i+9)%len(edges16)]<<16)] for i,x in enumerate(edges16)]
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
            body=re.search(r'Function : sem_'+family+r'\b(.*?)(?=Function :|\Z)',sass,re.S)[1]
            instructions={int(offset,16):mnemonic for offset,mnemonic in re.findall(
                r'/\*([0-9a-f]+)\*/\s+(?:@!?\w+\s+)?([A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*)',body)}
            executed={instructions[offset] for offset in result['visited']}
            for target in SPECS[family]:
                if not any(op==target or op.startswith(target+'.') for op in executed):
                    raise AssertionError(f'{family}: target {target} was never executed')
            actual=[int.from_bytes(bytes.fromhex(value),'little') for value in result['outputs']]
            if len(actual)!=len(gpu):raise RuntimeError(f'{family}: missing emulated outputs')
            for index,(g,e) in enumerate(zip(gpu,actual)):
                if family in HALF_FLOAT_OUTPUTS:
                    equal=all(((e>>shift)&0xffff)==((g>>shift)&0xffff) or
                        (((e>>shift)&0x7c00)==0x7c00 and ((e>>shift)&0x3ff)!=0 and
                         ((g>>shift)&0x7c00)==0x7c00 and ((g>>shift)&0x3ff)!=0) for shift in (0,16))
                    if not equal:raise AssertionError((family,index,vectors[index],hex(e),hex(g)))
                    continue
                if e!=g and not (family in FLOAT_OUTPUTS and nan(e,64 if family in DOUBLE_FAMILIES else 32) and nan(g,64 if family in DOUBLE_FAMILIES else 32)):
                    raise AssertionError((family,index,vectors[index],hex(e),hex(g)))
            print(f'{family}: {len(gpu)} whole-kernel GPU/p-code comparisons passed',flush=True)
            results.append(dict(family=family,passed=len(gpu),steps=result['steps'],visited=result['visited'],
                                executed_mnemonics=sorted(executed)))
        (out/'results.json').write_text(json.dumps(dict(mode='whole-kernel',architecture='SM89',
            seed=1,threads_per_kernel=128,patched_fixtures=patched,results=results,exclusions=['warp communication','cross-thread shared memory',
            'MUFU approximation'],floating_tolerance='bit-exact FSEL/FMNMX; other floating arithmetic except NaN payload/sign'),indent=2)+'\n')
    finally:driver.close()

if __name__=='__main__':main()
