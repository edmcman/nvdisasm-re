"""CUDA Driver API oracle for scalar Tier B p-code fixtures (SM89).

Compiles and checks target mnemonics even with --compile-only. Execution compares
GPU results to Ghidra's actual p-code for the corresponding scalar instruction;
this is not a kernel interpreter. Unknown selectors, synchronization, and MUFU
require their own hardware or environment tests.
"""
import argparse,ctypes as C,json,os,random,re,struct,subprocess,sys,tempfile
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from semantic_cases import encode
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT

SPECS={
 'iadd3':('IADD3','iadd3_noimm__RRR_RRR',{}),
 'imad':('IMAD','imad__RRR_RRR',{'fmt':'U32'}),
 'imad_hi':('IMAD','imad_hi__RRR_RRR',{'fmt':'U32'}),
 'lop3':('LOP3','lop3_lut__RRR_RRR',{'imm8':0x96,'Pp':'PT'}),
 'shf':('SHF','shf__RRR_RRR',{'dir':'R','cw':'W','fmt':'U32','hilo':'LO'}),
 'iabs':('IABS','iabs__RRR_R',{}),
 'imnmx':('IMNMX','imnmx__RRR_RRR',{'fmt':'U32','Pp':'PT'}),
 'prmt':('PRMT','prmt__RRR_RRR',{'pmode':'IDX'}),
 'fadd':('FADD','fadd__RRR_RR',{}),
 'fmul':('FMUL','fmul__RRR_RR',{}),
 'ffma':('FFMA','ffma__RRR_RRR',{})}

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
        a=self.malloc(len(data));n=len(data)//12;b=self.malloc(n*4)
        inp=C.create_string_buffer(data);result=C.create_string_buffer(n*4)
        self.call('cuMemcpyHtoD_v2',a,inp,C.c_size_t(len(data)))
        params=(C.c_void_p*2)(C.cast(C.byref(a),C.c_void_p),C.cast(C.byref(b),C.c_void_p))
        self.call('cuLaunchKernel',fn,C.c_uint(n//32),C.c_uint(1),C.c_uint(1),C.c_uint(32),C.c_uint(1),C.c_uint(1),C.c_uint(0),C.c_void_p(),params,C.c_void_p())
        self.call('cuCtxSynchronize');self.call('cuMemcpyDtoH_v2',result,b,C.c_size_t(n*4))
        self.call('cuModuleUnload',self.module);self.module=C.c_void_p()
        return struct.unpack('<'+'I'*n,result.raw)

def main():
    p=argparse.ArgumentParser();p.add_argument('--compile-only',action='store_true');p.add_argument('--require-gpu',action='store_true');p.add_argument('--output',default='/tmp/sass-gpu-oracle');a=p.parse_args()
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True);cubin=out/'sem_tierb.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler','-diag-suppress=177','-cubin','-arch=sm_89',str(ROOT/'tests/kernels/sem_tierb.cu'),'-o',str(cubin)],check=True)
    sass=subprocess.check_output([os.environ.get('CUOBJDUMP','/usr/local/cuda/bin/cuobjdump'),'-sass',str(cubin)],text=True);(out/'sem_tierb.sass').write_text(sass)
    for family,(mnemonic,_,_) in SPECS.items():
        match=re.search(r'Function : sem_'+family+r'\b(.*?)(?=Function :|\Z)',sass,re.S)
        if not match or not re.search(r'\b'+mnemonic+r'(?:\.|\s)',match[1]):raise RuntimeError('compiler did not emit '+mnemonic+' for '+family)
    print('Compiled and confirmed 11 scalar families in SM89 SASS')
    if a.compile_only:return
    try:driver=Driver()
    except (OSError,RuntimeError) as e:
        if a.require_gpu:raise
        print('SKIP GPU execution: '+str(e));return
    try:
        rng=random.Random(1);results=[]
        for family,(_,cls,extra) in SPECS.items():
            vectors=[[rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
            edges=[0,0xffffffff,0x80000000,0x7fffffff,0x7f800000,0xff800000,0x7fc00000,1]
            vectors[:len(edges)]=[[x,x,x] for x in edges]
            gpu=driver.run(cubin,family,b''.join(struct.pack('<III',*v) for v in vectors))
            fields=dict(Rd=8,Ra=2,Rb=4,Rc=6,**extra)
            if family=='iabs':fields=dict(Rd=8,Rb=2)
            if family=='fadd':fields=dict(Rd=8,Ra=2,Rc=4)
            word=encode('SM89',cls,**fields)
            requests=[dict(word=word,registers=(dict(R2=x,R4=y,R6=0,R7=z) if family=='imad_hi' else dict(R2=x,R4=y,R6=z)),observe=['R8']) for x,y,z in vectors]
            process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),'SASS:LE:64:sm89'],input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True)
            emulated=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
            if len(emulated)!=len(gpu):raise RuntimeError(process.stderr[-2000:])
            def nan(x):return x&0x7f800000==0x7f800000 and x&0x7fffff!=0
            for index,(g,result) in enumerate(zip(gpu,emulated)):
                if 'error' in result:raise RuntimeError(result['error'])
                e=result['registers']['R8']
                if e!=g and not (family.startswith('f') and nan(e) and nan(g)):
                    raise AssertionError((family,index,vectors[index],hex(e),hex(g)))
            print(f'{family}: {len(gpu)} GPU/p-code comparisons passed');results.append(dict(family=family,passed=len(gpu)))
        (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    finally:driver.close()

if __name__=='__main__':main()
