def library(language, call, interrupts=lambda name: False):
    import jpype
    from java.util import HashMap
    from ghidra.pcode.exec import PcodeUseropLibrary, PcodeExecutorStatePiece

    definition = jpype.JClass("ghidra.pcode.exec.PcodeUseropLibrary$PcodeUseropDefinition")
    reason = PcodeExecutorStatePiece.Reason.INSPECT
    refs = []

    class Userop:
        def __init__(self, name): self.name = name
        def getName(self): return self.name
        def getInputCount(self): return -1
        def isFunctional(self): return False
        def canInterrupt(self): return interrupts(self.name)
        def isOutSigned(self): return False
        def isInSigned(self, index): return False
        def hasSideEffects(self): return True
        def modifiesContext(self): return False
        def canInlinePcode(self): return False
        def getOutputType(self): return jpype.JArray(jpype.JByte).class_
        def getJavaMethod(self): return None
        def getDefiningLibrary(self): return proxy
        def execute(self, *parameters):
            if len(parameters) == 3:
                executor, _, op = parameters
                output, nodes = op.getOutput(), list(op.getInputs())[1:]
            else: executor, _, _, output, nodes = parameters
            state = executor.getState()
            arguments = [int.from_bytes(bytes(state.getVar(node, reason)), "little") for node in nodes]
            size = int(output.getSize()) if output else 0
            result = call(self.name, arguments, size, output)
            if output:
                result &= (1 << (size * 8)) - 1
                state.setVar(output, jpype.JArray(jpype.JByte)(result.to_bytes(size, "little")))

    operations = HashMap()
    for index in range(language.getNumberOfUserDefinedOpNames()):
        name = str(language.getUserDefinedOpName(index))
        obj = Userop(name)
        wrapper = jpype.JProxy(definition, inst=obj)
        refs.extend((obj, wrapper))
        operations.put(name, wrapper)
    proxy = jpype.JProxy(PcodeUseropLibrary, dict(getUserops=lambda: operations))
    return proxy, refs
