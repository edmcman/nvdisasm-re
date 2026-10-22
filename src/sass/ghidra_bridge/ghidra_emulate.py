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
from sass.semantics.runtime import Runtime

# Instructions reading warp state (collectives, BRA.DIV/.CONV): the runtime warp-context key and the
# operand each lane contributes (None: only the active and waiting masks).
WARP_INPUTS={'SHFL':('values','Ra'),'VOTE':('predicates','Pp'),'VOTEU':('predicates','Pp'),'MATCH':('values','Ra'),
             'REDUX':('values','Ra'),'ELECT':('values',None),'BRA':('values',None)}


def session(ldefs,langid):
    """A request handler (request dict -> result dict) for one language; the JVM must be running."""
    import jpype
    from java.util import HashMap,ArrayList
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.pcode.exec import PcodeUseropLibrary,PcodeExecutorStatePiece
    from ghidra.program.model.pcode import Varnode
    lang=load_language(ldefs,langid);space=lang.getDefaultSpace();variant=langid.rsplit(':',1)[1];arch=variant.split('_')[0].upper();runtime=Runtime(Path(ldefs).parent,int(arch[2:]),variant=variant)
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
    def operand(state,d,name):
        """A decoded register operand's value in a thread's state, over its md span (MATCH.U64's
        pair); the zero registers (RZ, PT, ...) are hardwired in p-code, not stored, so they read as
        their constants."""
        from sass.decode import ir
        from sass.md import expression
        from sass.semantics.family import roles
        from sass.sleigh.registers import ENUM_FILE, REG_FILES
        file=ENUM_FILE[d.klass.operand_types[name].type];zero=REG_FILES[file][3]
        if d.env[name]==zero:return int(file in ('P','UP'))
        span=next((o['span'] for o in roles(arch)[d.klass.name]['operands'] if o['name']==name),1) if d.klass.name in roles(arch) else 1
        span=int(expression.evaluate(span,ir.load(arch),d.env)) if isinstance(span,str) else span
        return int.from_bytes(b''.join(bytes(state.getVar(lang.getRegister(file+str(d.env[name]+k)),reason)) for k in range(span)),'little')
    def run_kernel(request):
        """Threads start at the kernel entry and run to EXIT, each with its own emulator. With
        kernel.cooperative, a block shares one emulator (BlockEmulator: common memory, private
        localmem); its warps take turns, each turn stepping the live lanes at the warp's lowest PC
        together (they form the active mask; a warp collective first gathers each lane's operand,
        WARP_INPUTS), and BAR.SYNC holds a thread until every live thread of the block arrives
        (barrier ids and thread counts are not modelled). WARPSYNC holds a lane until every live lane
        its mask names waits at a WARPSYNC with the same mask (CUDA's __syncwarp contract; exited
        lanes are not waited for). Blocks run one after another."""
        from sass.decode import ir,pydecode
        kernel=request['kernel'];code=bytes.fromhex(kernel['code']);base=int(kernel.get('address',0x100000))
        count=int(kernel['threads']);block=int(kernel.get('block_size',32));outputs=[];steps=0;visited=set()
        max_steps=int(kernel.get('max_steps',10000));name=kernel.get('name');cooperative=bool(kernel.get('cooperative'))
        trace=[] if kernel.get('trace') else None;traced=int(kernel.get('trace_threads',1))
        if count<=0 or block<=0 or max_steps<=0 or not code or len(code)%16:
            raise ValueError('invalid kernel launch or instruction text')
        decoded={}
        def decode(pc):
            if pc not in decoded:decoded[pc]=pydecode.decode(arch,code[pc-base:pc-base+16])
            return decoded[pc]
        def start(emulator,lane,first):
            thread=emulator.newThread(f'thread {lane}');state=thread.getState()
            library_field.set(thread,thread.getUseropLibrary().compose(lib))
            if first:write_memory(state,request.get('memory',[])+[dict(space='ram',address=base,hex=code.hex())])
            thread.overrideCounter(space.getAddress(base))
            # Registers start at Ghidra's default zero value. Seed only launch
            # inputs; hardwired RZ/PT come from the generated language.
            for reg,value in {**kernel.get('registers',{}),'SR_TID.X':lane%block,'SR_TID.Y':0,'SR_TID.Z':0,'SR_CTAID.X':lane//block,'SR_CTAID.Y':0,'SR_CTAID.Z':0}.items():
                state.setVar(lang.getRegister(reg),jpype.JArray(jpype.JByte)(value.to_bytes(4,'little')))
            return dict(lane=lane,thread=thread,state=state,last=base,steps=0,waiting=False,released=False,sync=None)
        counter=lambda t:int(t['thread'].getCounter().getOffset())
        def value(state,r):return int.from_bytes(bytes(state.getVar(r,reason)),'little')
        named={int(r.getOffset()):(n,r) for n,r in architectural};written={}
        def destinations(pc):
            """The architectural registers the instruction at pc writes: its p-code's register outputs."""
            if pc not in written:
                outs=[(int(v.getOffset()),int(v.getSize())) for op in parse(lang,space.getAddress(pc),code[pc-base:pc-base+16]).getPcode()
                      if (v:=op.getOutput()) is not None and v.isRegister()]
                written[pc]=list(dict.fromkeys(named[o+k] for o,size in outs for k in range(size) if o+k in named))
            return written[pc]
        def step(t,pc,context):
            if t['steps']>=max_steps:raise RuntimeError(f'kernel instruction limit ({max_steps}) at {pc:#x}, thread {t["lane"]}')
            runtime.context=context;runtime.events=[];runtime.last_error=None;t['last']=pc
            traced_step=trace is not None and t['lane']<traced
            # A write is reported even when it leaves the value unchanged; a predicated-off instruction writes nothing.
            d=decode(pc);taken=traced_step and ('Pg' not in d.klass.operand_types or operand(t['state'],d,'Pg')^d.env.get(('Pg','not'),0))
            try:t['thread'].stepInstruction()
            except Exception as error:
                runtime.last_error=f'kernel {name} thread {t["lane"]} at {pc-base:#x}: {runtime.last_error or error}'
                raise RuntimeError(runtime.last_error) from error
            t['steps']+=1
            if traced_step:
                trace.append(dict(thread=t['lane'],pc=pc-base,text=decode(pc).text,
                                  writes={n:value(t['state'],r) for n,r in destinations(pc)} if taken else {}))
        def sync_mask(t,d):
            """The member mask of t's WARPSYNC; EXCLUSIVE, COLLECTIVE and constant-bank forms are not modelled."""
            k=d.klass;labels={ir.load(arch).rev_enums[k.operand_types[n].type][d.env[n]] for n in ('ex','div','all') if n in d.env}
            if k.name not in ('warpsync__RIR','warpsync__RRR','warpsync__RUR') or labels-{'noexclusive','nodiv__exclusive','ALL'}:
                raise RuntimeError(f'kernel {name}: {d.text} is not modelled')
            if 'ALL' in labels:return 0xffffffff
            source=next(n for n in ('Ra','Rb','URb','Sb') if n in k.operand_types)
            mask=(d.env[source] if source=='Sb' else operand(t['state'],d,source))&0xffffffff
            if not mask>>t['lane']%32&1:raise RuntimeError(f'kernel {name} thread {t["lane"]}: {d.text} mask {mask:#x} omits the lane')
            return mask
        def release_warpsyncs(warp):
            for t in warp:
                if t['sync'] is None:continue
                members=[u for u in warp if t['sync']>>u['lane']%32&1 and counter(u)!=0]
                if any(u['sync'] is None for u in members):continue
                if any(u['sync']!=t['sync'] for u in members):
                    raise RuntimeError(f'kernel {name}: WARPSYNC masks {t["sync"]:#x} and {next(u["sync"] for u in members if u["sync"]!=t["sync"]):#x} disagree')
                for u in members:u['waiting'],u['released'],u['sync']=False,True,None
        def turn(warp):
            """One lockstep instruction of a warp; False if none of its lanes could move."""
            release_warpsyncs(warp)
            live=[t for t in warp if counter(t)!=0 and not t['waiting']]
            if not live:return False
            pc=min(map(counter,live));active=[t for t in live if counter(t)==pc]
            if not base<=pc<base+len(code) or (pc-base)%16:
                raise RuntimeError(f'kernel {name} thread {active[0]["lane"]}: PC outside kernel {pc:#x}')
            inputs=None;visited.add(pc-base);d=decode(pc);context=dict(request.get('context',{}))
            guarded=cooperative and 'Pg' in d.klass.operand_types
            taking=[t for t in active if operand(t['state'],d,'Pg')^d.env.get(('Pg','not'),0)] if guarded else active
            if cooperative and d.klass.name.startswith('bar__SYNC') and not all(t['released'] for t in taking):
                for t in taking:t['waiting']=True
                active=[t for t in active if t not in taking]
            elif cooperative and d.klass.mnemonic=='WARPSYNC':
                held=[t for t in taking if not t['released'] and operand(t['state'],d,'Pp')^d.env.get(('Pp','not'),0)]
                for t in held:t['waiting'],t['sync']=True,sync_mask(t,d)
                active=[t for t in active if t not in held]
            elif inputs:=WARP_INPUTS.get(d.klass.mnemonic) if cooperative else None:
                key,source=inputs;values=[0]*32
                for t in taking:values[t['lane']%32]=operand(t['state'],d,source) if source else 0
                # waiting: lanes still alive that are not at this instruction (BRA.DIV/.CONV)
                context['warp']={'active':sum(1<<t['lane']%32 for t in taking),key:values,
                                 'waiting':sum(1<<t['lane']%32 for t in warp if counter(t)!=0 and t not in active)}
            for t in active:
                step(t,pc,dict(context,warp=dict(context['warp'],lane=t['lane']%32)) if inputs else context)
                t['released']=False
            return True
        if cooperative and block%32:raise ValueError('cooperative execution requires whole 32-thread warps')
        for group in ([range(b,min(b+block,count)) for b in range(0,count,block)] if cooperative else [[t] for t in range(count)]):
            emulator=BlockEmulator(lang) if cooperative else PcodeEmulator(lang)
            lanes=[start(emulator,t,i==0 or not cooperative) for i,t in enumerate(group)]
            warps=[lanes[i:i+32] for i in range(0,len(lanes),32)]
            while live:=[t for t in lanes if counter(t)!=0]:
                if all(t['waiting'] and t['sync'] is None for t in live):
                    for t in live:t['waiting'],t['released']=False,True
                if not any([turn(w) for w in warps]):
                    raise RuntimeError(f'kernel {name}: no thread can proceed')
            steps+=sum(t['steps'] for t in lanes)
            for t in lanes:
                if decode(t['last']).klass.mnemonic!='EXIT':raise RuntimeError('kernel terminated without EXIT')
                out=request['output'];offset=int(out['address'])+t['lane']*int(out.get('stride',4));size=int(out.get('size',4))
                outputs.append(bytes(t['state'].getVar(Varnode(space.getAddress(offset),size),reason)).hex())
        return dict(outputs=outputs,threads=count,steps=steps,visited=sorted(visited),exit='EXIT',**({'trace':trace} if trace is not None else {}))
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
        ops=ins.getPcode();program=ArrayList()
        for op in ops:program.add(op)
        # Execute with the actual Ghidra byte arithmetic and state machinery.
        thread.overrideCounter(space.getAddress(address+16))
        thread.getExecutor().execute(program,HashMap(),lib)
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
        try:return run_kernel(request) if 'kernel' in request else instruction(request)
        except Exception as error:
            import traceback
            traceback.print_exc(file=sys.stderr)
            return {'error':runtime.last_error or str(error)}
    return handle


def main(ldefs,langid):
    pyghidra.start(install_dir=GHIDRA)
    handle=session(ldefs,langid)
    for line in sys.stdin:print(json.dumps(handle(json.loads(line))),flush=True)


if __name__=='__main__':main(*sys.argv[1:])
