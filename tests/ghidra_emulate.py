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
    lang=load_language(ldefs,langid);space=lang.getDefaultSpace();runtime=Runtime(Path(ldefs).parent)
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
    for line in sys.stdin:
        request=json.loads(line)
        try:
            emulator=PcodeEmulator(lang);thread=emulator.newThread();state=thread.getState()
            address=int(request.get('address',0x1000));addr=space.getAddress(address)
            thread.overrideCounter(addr)
            runtime.context=request.get('context',{});runtime.events=[];runtime.last_error=None
            # Initialize architectural registers, so tests may also inspect untouched ones.
            for file,count,size in (('R',256,4),('UR',64,4),('P',8,1),('UP',8,1)):
                for i in range(count):
                    name=('RZ' if file=='R' else 'URZ' if file=='UR' else 'PT' if file=='P' else 'UPT') if i==count-1 else file+str(i)
                    reg=lang.getRegister(name)
                    initial=1 if name in ('PT','UPT') else 0
                    state.setVar(reg,jpype.JArray(jpype.JByte)(initial.to_bytes(size,'little')))
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
        except Exception as error:out={'error':runtime.last_error or str(error)}
        print(json.dumps(out),flush=True)


if __name__=='__main__':main(*sys.argv[1:])
