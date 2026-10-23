"""Execute generated instruction p-code with Ghidra's PcodeEmulator.

Run under pyghidra's Python. stdin JSON lines: word, address, registers,
memory [{space,address,hex}], observe registers/memory, optional runtime context; or a whole-kernel
request (`sass.launch.kernel_request`), whose kernel.trace adds each step's register writes for
the first kernel.trace_threads threads. In-process callers start the JVM and use `session`.
"""
import json,sys
from collections import Counter
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parents[2])]
import pyghidra
from ghidra_decode import GHIDRA,java_class,load_language,parse
from sass.emulate import schedule
from sass.semantics.runtime import Runtime


def session(ldefs,langid):
    """A request handler (request dict -> result dict) for one language; the JVM must be running."""
    import jpype
    from java.util import HashMap
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.pcode.exec import PcodeProgram,PcodeUseropLibrary,PcodeExecutorStatePiece
    from ghidra.program.model.pcode import Varnode
    lang=load_language(ldefs,langid);space=lang.getDefaultSpace();variant=langid.rsplit(':',1)[1];arch=variant.split('_')[0].upper();runtime=Runtime(int(arch[2:]))
    reason=PcodeExecutorStatePiece.Reason.INSPECT
    from sass.sleigh.registers import REG_FILES
    architectural=[(name,lang.getRegister(name)) for file,(_,_,count,zero,_) in REG_FILES.items() for i in range(count) if i!=zero for name in [file+str(i)]]
    definition=jpype.JClass('ghidra.pcode.exec.PcodeUseropLibrary$PcodeUseropDefinition')
    refs=[]
    class Userop:
        def __init__(self,name):self.name=name
        def getName(self):return self.name
        def getInputCount(self):return -1
        def isFunctional(self):return False
        def canInterrupt(self):return False
        def isOutSigned(self):return False
        def isInSigned(self,i):return False
        def hasSideEffects(self):return True
        def modifiesContext(self):return False
        def canInlinePcode(self):return False
        def getOutputType(self):return jpype.JArray(jpype.JByte).class_
        def getJavaMethod(self):return None
        def getDefiningLibrary(self):return lib
        def execute(self,*params):
            if len(params)==3:
                executor,library,op=params;output=op.getOutput();nodes=list(op.getInputs())[1:]
            else:executor,library,op,output,nodes=params
            state=executor.getState()
            args=[int.from_bytes(bytes(state.getVar(n,reason)),'little') for n in nodes]
            size=int(output.getSize()) if output else 0
            try:result=runtime.call(self.name,args,size)
            except Exception as error:
                runtime.last_error=str(error);raise
            if output:
                result&=(1<<(size*8))-1
                state.setVar(output,jpype.JArray(jpype.JByte)(result.to_bytes(size,'little')))
    ops=HashMap()
    for i in range(lang.getNumberOfUserDefinedOpNames()):
        name=lang.getUserDefinedOpName(i)
        obj=Userop(str(name));proxy=jpype.JProxy(definition,inst=obj);refs.extend((obj,proxy));ops.put(name,proxy)
    lib=jpype.JProxy(PcodeUseropLibrary,dict(getUserops=lambda:ops))
    # Attach the runtime to Ghidra's normal fetch/decode/execute loop. The thread
    # library field is protected; composing it preserves Ghidra's built-in ops.
    thread_class=jpype.JClass('ghidra.pcode.emu.DefaultPcodeThread')
    library_field=thread_class.class_.getDeclaredField('library');library_field.setAccessible(True)
    BlockEmulator=java_class('BlockEmulator')
    def write_memory(state,memory):
        for mem in memory:
            sp=lang.getAddressFactory().getAddressSpace(mem['space']);data=bytes.fromhex(mem['hex'])
            state.setVar(Varnode(sp.getAddress(mem['address']),len(data)),jpype.JArray(jpype.JByte)(data))
    def value(state,r):return int.from_bytes(bytes(state.getVar(r,reason)),'little')
    named={int(r.getOffset()):(n,r) for n,r in architectural}
    class GhidraLane:
        """A thread of a Ghidra emulator, stepped by `schedule.run_kernel`."""
        def __init__(self,kernel,thread):self.kernel,self.thread,self.state=kernel,thread,thread.getState()
        def pc(self):return int(self.thread.getCounter().getOffset())
        def read(self,file,index):return value(self.state,lang.getRegister(file+str(index)))
        def seed(self,name,v):self.state.setVar(lang.getRegister(name),jpype.JArray(jpype.JByte)(v.to_bytes(4,'little')))
        def step(self,decoded,context):
            runtime.context=context;runtime.events=[];runtime.last_error=None
            try:self.thread.stepInstruction()
            except Exception as error:raise schedule.Fault(runtime.last_error or str(error)) from error
        def writes(self,pc):return {n:value(self.state,r) for n,r in self.kernel.destinations(pc)}
    class KernelEngine:
        """One request's emulators: a BlockEmulator (common memory, private localmem) per cooperative
        block, a PcodeEmulator per thread otherwise."""
        def __init__(self,request):
            kernel=request['kernel'];self.code=bytes.fromhex(kernel['code']);self.base=int(kernel.get('address',0x100000))
            self.memory=request.get('memory',[])+[dict(space='ram',address=self.base,hex=self.code.hex())];self.written={}
        def group(self,threads,cooperative):
            emulator=BlockEmulator(lang) if cooperative else PcodeEmulator(lang);lanes=[]
            for i,t in enumerate(threads):
                thread=emulator.newThread(f'thread {t}')
                library_field.set(thread,thread.getUseropLibrary().compose(lib))
                if i==0:write_memory(thread.getState(),self.memory)
                thread.overrideCounter(space.getAddress(self.base));lanes.append(GhidraLane(self,thread))
            return lanes
        def output(self,lane,address,size):return bytes(lane.state.getVar(Varnode(space.getAddress(address),size),reason))
        def destinations(self,pc):
            """The architectural registers the instruction at pc writes: its p-code's register outputs."""
            if pc not in self.written:
                outs=[(int(v.getOffset()),int(v.getSize())) for op in parse(lang,space.getAddress(pc),self.code[pc-self.base:pc-self.base+16]).getPcode()
                      if (v:=op.getOutput()) is not None and v.isRegister()]
                self.written[pc]=list(dict.fromkeys(named[o+k] for o,size in outs for k in range(size) if o+k in named))
            return self.written[pc]
    true_registers=[(lang.getRegister(name),jpype.JArray(jpype.JByte)(b'\x01'+bytes(REG_FILES[file][1]-1)))
                    for file,name in (('P','PT'),('UP','UPT')) if file in REG_FILES]
    def instruction(request):
        emulator=PcodeEmulator(lang);thread=emulator.newThread();state=thread.getState()
        address=int(request.get('address',0x1000));addr=space.getAddress(address)
        thread.overrideCounter(addr)
        runtime.context=request.get('context',{});runtime.events=[];runtime.last_error=None
        # Architectural registers start at Ghidra's default zero value, so tests may also inspect
        # untouched ones; only PT/UPT start nonzero.
        for reg,value in true_registers:state.setVar(reg,value)
        for name,value in request.get('registers',{}).items():
            reg=lang.getRegister(name);size=int(reg.getMinimumByteSize());value=int(value)&((1<<(size*8))-1)
            if name in ('RZ','URZ'):value=0
            if name in ('PT','UPT'):value=1
            state.setVar(reg,jpype.JArray(jpype.JByte)(value.to_bytes(size,'little')))
        write_memory(state,request.get('memory',[]))
        ins=parse(lang,addr,bytes.fromhex(request['word']))
        program=PcodeProgram.fromInstruction(ins,False);ops=program.getCode()
        # Execute with the actual Ghidra byte arithmetic and state machinery.
        thread.overrideCounter(space.getAddress(address+16))
        # Composed, as in kernels: Java then runs the default methods (12.2's getSymbols), which JPype 1.5.2 proxies cannot.
        thread.getExecutor().execute(program,thread.getUseropLibrary().compose(lib))
        registers={}
        for name in request.get('observe',[]):
            registers[name]=int.from_bytes(bytes(state.getVar(lang.getRegister(name),reason)),'little')
        memories=[]
        for mem in request.get('observe_memory',[]):
            sp=lang.getAddressFactory().getAddressSpace(mem['space'])
            data=bytes(state.getVar(Varnode(sp.getAddress(mem['address']),mem['size']),reason))
            memories.append(dict(mem,hex=data.hex()))
        out=dict(text=str(ins),registers=registers,memory=memories,counter=int(thread.getCounter().getOffset()),events=runtime.events)
        if request.get('inspect_pcode'):
            out['pcode_ops']=dict(Counter(str(op.getMnemonic()) for op in ops))
            out['pcode_userops']=[str(lang.getUserDefinedOpName(int(op.getInput(0).getOffset())))
                                  for op in ops if str(op.getMnemonic())=='CALLOTHER']
        return out
    def handle(request):
        try:return schedule.run_kernel(request,KernelEngine(request),arch) if 'kernel' in request else instruction(request)
        except Exception as error:
            import traceback
            traceback.print_exc(file=sys.stderr)
            # The scheduler's message names the kernel, thread and offset around the runtime's.
            return {'error':str(error) if 'kernel' in request else runtime.last_error or str(error)}
        finally:
            # Ghidra 12.2's weak SYMBOL_CACHE values hold their keys, so every library an executor ran
            # (each thread's, with its emulator) would stay reachable: 4-7 GB a session in CI's fixtures.
            PcodeUseropLibrary.SYMBOL_CACHE.clear()
    return handle


def main(ldefs,langid):
    pyghidra.start(install_dir=GHIDRA)
    handle=session(ldefs,langid)
    for line in sys.stdin:print(json.dumps(handle(json.loads(line))),flush=True)


if __name__=='__main__':main(*sys.argv[1:])
