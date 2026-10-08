"""Execute generated instruction p-code with Ghidra's PcodeEmulator.

Run under pyghidra's Python. stdin JSON lines: word, address, registers,
memory [{space,address,hex}], observe registers/memory, optional runtime context.
"""
import json,sys,os
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
import pyghidra
from ghidra_decode import GHIDRA,load_language
from sass.runtime import Runtime


def main(ldefs,langid):
    pyghidra.start(install_dir=GHIDRA)
    import jpype
    from java.util import HashMap,ArrayList
    from java.lang import Class
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.pcode.exec import PcodeUseropLibrary,PcodeExecutorStatePiece
    from ghidra.program.model.lang import ProcessorContextImpl
    from ghidra.program.model.mem import ByteMemBufferImpl
    from ghidra.app.util import PseudoInstruction
    from ghidra.program.model.pcode import Varnode
    lang=load_language(ldefs,langid);space=lang.getDefaultSpace();variant=langid.rsplit(':',1)[1];arch=variant.split('_')[0].upper();runtime=Runtime(Path(ldefs).parent,int(arch[2:]),variant=variant)
    reason=PcodeExecutorStatePiece.Reason.INSPECT
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
    def write_memory(state,memory):
        for mem in memory:
            sp=lang.getAddressFactory().getAddressSpace(mem['space']);data=bytes.fromhex(mem['hex'])
            state.setVar(Varnode(sp.getAddress(mem['address']),len(data)),jpype.JArray(jpype.JByte)(data))
    def run_kernel(request):
        from sass import pydecode
        kernel=request['kernel'];code=bytes.fromhex(kernel['code']);base=int(kernel.get('address',0x100000))
        count=int(kernel['threads']);block=int(kernel.get('block_size',32));outputs=[];steps=0;visited=set()
        max_steps=int(kernel.get('max_steps',10000));current_pc=base
        if count<=0 or block<=0 or max_steps<=0 or not code or len(code)%16:
            raise ValueError('invalid kernel launch or instruction text')
        for lane in range(count):
            last_pc=base
            emulator=PcodeEmulator(lang);thread=emulator.newThread();state=thread.getState()
            library_field.set(thread,thread.getUseropLibrary().compose(lib))
            memory=request.get('memory',[])+[dict(space='ram',address=base,hex=code.hex())]
            write_memory(state,memory);thread.overrideCounter(space.getAddress(base))
            # Registers start at Ghidra's default zero value. Seed only launch
            # inputs; hardwired RZ/PT come from the generated language.
            runtime.context=dict(request.get('context',{}));runtime.events=[];runtime.last_error=None
            for name,value in {'SR_TID.X':lane%block,'SR_TID.Y':0,'SR_TID.Z':0,'SR_CTAID.X':lane//block,'SR_CTAID.Y':0,'SR_CTAID.Z':0}.items():
                state.setVar(lang.getRegister(name),jpype.JArray(jpype.JByte)(value.to_bytes(4,'little')))
            for iteration in range(max_steps):
                current_pc=int(thread.getCounter().getOffset())
                if current_pc==0:break
                if not base<=current_pc<base+len(code) or (current_pc-base)%16:
                    raise RuntimeError(f'kernel {kernel.get("name")} thread {lane}: PC outside kernel {current_pc:#x}')
                visited.add(current_pc-base)
                last_pc=current_pc
                try:thread.stepInstruction()
                except Exception as error:
                    runtime.last_error=f'kernel {kernel.get("name")} thread {lane} at {current_pc-base:#x}: {runtime.last_error or error}'
                    raise RuntimeError(runtime.last_error) from error
                steps+=1
            else:
                if int(thread.getCounter().getOffset())!=0:
                    raise RuntimeError(f'kernel instruction limit ({max_steps}) at {current_pc:#x}, thread {lane}')
            last=pydecode.decode(arch,code[last_pc-base:last_pc-base+16])
            if last.klass.mnemonic!='EXIT':raise RuntimeError('kernel terminated without EXIT')
            out=request['output'];offset=int(out['address'])+lane*int(out.get('stride',4));size=int(out.get('size',4))
            outputs.append(bytes(state.getVar(Varnode(space.getAddress(offset),size),reason)).hex())
        return dict(outputs=outputs,threads=count,steps=steps,visited=sorted(visited),exit='EXIT')
    for line in sys.stdin:
        request=json.loads(line)
        try:
            if 'kernel' in request:
                out=run_kernel(request);print(json.dumps(out),flush=True);continue
            emulator=PcodeEmulator(lang);thread=emulator.newThread();state=thread.getState()
            address=int(request.get('address',0x1000));addr=space.getAddress(address)
            thread.overrideCounter(addr)
            runtime.context=request.get('context',{});runtime.events=[];runtime.last_error=None
            # Initialize architectural registers, so tests may also inspect untouched ones.
            from sass.gen_sleigh import REG_FILES
            for file,(_,size,count,zero,zero_name) in REG_FILES.items():
                for i in range(count):
                    name=zero_name if i==zero else file+str(i)
                    reg=lang.getRegister(name)
                    initial=1 if name in ('PT','UPT') else 0
                    state.setVar(reg,jpype.JArray(jpype.JByte)(initial.to_bytes(size,'little')))
                if zero>=count:
                    state.setVar(lang.getRegister(zero_name),jpype.JArray(jpype.JByte)(bytes(size)))
            for name,value in request.get('registers',{}).items():
                reg=lang.getRegister(name);size=int(reg.getMinimumByteSize());value=int(value)&((1<<(size*8))-1)
                if name in ('RZ','URZ'):value=0
                if name in ('PT','UPT'):value=1
                state.setVar(reg,jpype.JArray(jpype.JByte)(value.to_bytes(size,'little')))
            for mem in request.get('memory',[]):
                sp=lang.getAddressFactory().getAddressSpace(mem['space']);data=bytes.fromhex(mem['hex'])
                state.setVar(Varnode(sp.getAddress(mem['address']),len(data)),jpype.JArray(jpype.JByte)(data))
            word=bytes.fromhex(request['word']);buf=ByteMemBufferImpl(addr,word,False);ctx=ProcessorContextImpl(lang)
            ins=PseudoInstruction(addr,lang.parse(buf,ctx,False),buf,ctx)
            program=ArrayList()
            for op in ins.getPcode():program.add(op)
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
                out['pcode_ops']=[str(op.getMnemonic()) for op in ins.getPcode()]
        except Exception as error:
            import traceback
            traceback.print_exc(file=sys.stderr)
            out={'error':runtime.last_error or str(error)}
        print(json.dumps(out),flush=True)


if __name__=='__main__':main(*sys.argv[1:])
