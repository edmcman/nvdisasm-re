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
from sass.semantics import ordered_runtime


def session(ldefs,langid):
    """A request handler (request dict -> result dict) for one language; the JVM must be running."""
    import jpype
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.pcode.exec import PcodeProgram,PcodeUseropLibrary,PcodeExecutorStatePiece
    from ghidra.program.model.pcode import Varnode
    lang=load_language(ldefs,langid);space=lang.getDefaultSpace();variant=langid.rsplit(':',1)[1];arch=variant.split('_')[0].upper();runtime=Runtime(int(arch[2:]))
    reason=PcodeExecutorStatePiece.Reason.INSPECT
    from sass.sleigh.registers import REG_FILES
    aliases={file+str(zero):name for file,(_,_,_,zero,name) in REG_FILES.items()}
    hardwired={name:int(file in ('P','UP')) for file,(_,_,_,_,name) in REG_FILES.items()}
    def register(name):
        reg=lang.getRegister(aliases.get(name,name))
        if reg is None:raise ValueError(f'unknown register {name}')
        return reg
    def read(state,name):
        canonical=aliases.get(name,name)
        return hardwired[canonical] if canonical in hardwired else value(state,register(canonical))
    def seed(state,name,v):
        canonical=aliases.get(name,name);reg=register(canonical);size=int(reg.getMinimumByteSize())
        v=hardwired.get(canonical,int(v))&((1<<(size*8))-1)
        state.setVar(reg,jpype.JArray(jpype.JByte)(v.to_bytes(size,'little')))
    architectural=[(name,lang.getRegister(name)) for file,(_,_,count,zero,_) in REG_FILES.items() for i in range(count) if i!=zero for name in [file+str(i)]]
    from sass.ghidra_bridge import ordered, userops
    active_execution = None
    def call(name,args,size,output):
        try:
            if name.startswith(ordered.PREFIX):
                if active_execution is None: raise RuntimeError("ordered operation outside execution")
                return active_execution.call(name,args,output)
            return runtime.call(name,args,size)
        except Exception as error:
            runtime.last_error=str(error);raise
    def interrupts(name):
        from sass.semantics import ordered
        spec=ordered.OPERATIONS.get(name.removeprefix("sass_ordered_"))
        return bool(spec and spec.suspends)
    lib,refs=userops.library(lang,call,interrupts)
    # Attach the runtime to Ghidra's normal fetch/decode/execute loop. The thread
    # library field is protected; composing it preserves Ghidra's built-in ops.
    thread_class=jpype.JClass('ghidra.pcode.emu.DefaultPcodeThread')
    library_field=thread_class.class_.getDeclaredField('library');library_field.setAccessible(True)
    BlockEmulator=java_class('BlockEmulator')
    def location(space,offset):
        offset=int(offset)&((1<<64)-1)
        return space.getAddress(offset if offset<1<<63 else offset-(1<<64))
    def write_memory(state,memory):
        for mem in memory:
            sp=lang.getAddressFactory().getAddressSpace(mem['space']);data=bytes.fromhex(mem['hex'])
            state.setVar(Varnode(location(sp,mem['address']),len(data)),jpype.JArray(jpype.JByte)(data))
    def value(state,r):return int.from_bytes(bytes(state.getVar(r,reason)),'little')
    named={int(r.getOffset()):(n,r) for n,r in architectural}
    class GhidraLane:
        """A thread of a Ghidra emulator, stepped by `schedule.run_kernel`."""
        def __init__(self,kernel,thread,runtime_,index):
            self.kernel,self.thread,self.state=kernel,thread,thread.getState()
            self.runtime,self.index,self.execution,self.progressed=runtime_,index,None,False
        def pc(self):return int(self.thread.getCounter().getOffset())&((1<<64)-1)
        def read(self,file,index):return read(self.state,file+str(index))
        def store(self,space,address,data):write_memory(self.state,[dict(space=space,address=address,hex=data.hex())])
        def seed(self,name,v):seed(self.state,name,v)
        def step(self,decoded,context):
            nonlocal active_execution
            runtime.context=context;runtime.events=[];runtime.last_error=None
            if self.execution is None:self.execution=ordered.Execution(self.thread,self.runtime)
            execution=self.execution
            local=self.index%self.kernel.block
            self.runtime.bind(execution,local%32,(local//32,self.pc()),participant=self.index)
            frame=self.thread.getFrame()
            before=(None if frame is None else int(frame.index()),execution.pending)
            active_execution=execution
            try:
                status=execution.step()
                frame=self.thread.getFrame()
                self.progressed=before!=(None if frame is None else int(frame.index()),execution.pending)
                if status.value=="fault":raise schedule.Fault(runtime.last_error or str(execution.error))
                if status.value=="complete":self.execution=None
                return status
            finally:active_execution=None
        def writes(self,pc):return {n:value(self.state,r) for n,r in self.kernel.destinations(pc)}
    class KernelEngine:
        """One request's emulators: a BlockEmulator (common memory, private localmem) per interacting
        block, a PcodeEmulator per thread otherwise."""
        def __init__(self,request):
            kernel=request['kernel'];self.code=bytes.fromhex(kernel['code']);self.base=int(kernel.get('address',0x100000))
            self.block=schedule.launch(kernel)[3]
            self.memory=request.get('memory',[])+[dict(space='ram',address=self.base,hex=self.code.hex())];self.written={}
            self.global_state=PcodeEmulator(lang).getSharedState() if kernel.get('scope','launch')=='launch' else None
            if self.global_state is not None:write_memory(self.global_state,[m for m in self.memory if m['space'] not in ('shared','localmem')])
        def group(self,threads,scope):
            match scope:
                case 'launch':emulator=BlockEmulator(lang,self.global_state)
                case 'block':emulator=BlockEmulator(lang)
                case _:emulator=PcodeEmulator(lang)
            lanes=[]
            cooperative=ordered_runtime.Cooperative(provider=runtime)
            for i,t in enumerate(threads):
                thread=emulator.newThread(f'thread {t}')
                library_field.set(thread,thread.getUseropLibrary().compose(lib))
                write_memory(thread.getState(),[m for m in self.memory if m['space']=='localmem' or i==0 and
                             (scope!='launch' or m['space']=='shared')])
                thread.overrideCounter(location(space,self.base));lanes.append(GhidraLane(self,thread,cooperative,t))
            return lanes
        def output(self,lane,address,size):return bytes(lane.state.getVar(Varnode(location(space,address),size),reason))
        def destinations(self,pc):
            """The architectural registers the instruction at pc writes: its p-code's register outputs."""
            if pc not in self.written:
                outs=[(int(v.getOffset()),int(v.getSize())) for op in parse(lang,location(space,pc),self.code[pc-self.base:pc-self.base+16]).getPcode()
                      if (v:=op.getOutput()) is not None and v.isRegister()]
                self.written[pc]=list(dict.fromkeys(named[o+k] for o,size in outs for k in range(size) if o+k in named))
            return self.written[pc]
    true_registers=[(lang.getRegister(name),jpype.JArray(jpype.JByte)(b'\x01'+bytes(REG_FILES[file][1]-1)))
                    for file,name in (('P','PT'),('UP','UPT')) if file in REG_FILES]
    def instruction(request):
        nonlocal active_execution
        emulator=PcodeEmulator(lang);thread=emulator.newThread();state=thread.getState()
        address=int(request.get('address',0x1000));addr=location(space,address)
        thread.overrideCounter(addr)
        runtime.context=request.get('context',{});runtime.events=[];runtime.last_error=None
        # Architectural registers start at Ghidra's default zero value, so tests may also inspect
        # untouched ones; only PT/UPT start nonzero.
        for reg,value in true_registers:state.setVar(reg,value)
        for name,value in request.get('registers',{}).items():
            seed(state,name,value)
        write_memory(state,request.get('memory',[]))
        ins=parse(lang,addr,bytes.fromhex(request['word']))
        program=PcodeProgram.fromInstruction(ins,False);ops=program.getCode()
        # Execute with the actual Ghidra byte arithmetic and state machinery.
        thread.overrideCounter(location(space,address+16))
        # Composed, as in kernels: Java then runs the default methods (12.2's getSymbols), which JPype 1.5.2 proxies cannot.
        active_execution=ordered.Execution(thread,ordered_runtime.Explicit(provider=runtime))
        try:thread.getExecutor().execute(program,thread.getUseropLibrary().compose(lib))
        finally:
            active_execution.release()
            active_execution=None
        registers={}
        for name in request.get('observe',[]):
            registers[name]=read(state,name)
        memories=[]
        for mem in request.get('observe_memory',[]):
            sp=lang.getAddressFactory().getAddressSpace(mem['space'])
            data=bytes(state.getVar(Varnode(location(sp,mem['address']),mem['size']),reason))
            memories.append(dict(mem,hex=data.hex()))
        out=dict(text=str(ins),registers=registers,memory=memories,counter=int(thread.getCounter().getOffset())&((1<<64)-1),events=runtime.events)
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
