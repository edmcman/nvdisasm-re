"""Decode instruction words with a SLEIGH language straight from its .ldefs (no project, no install).

Runs under Ghidra's pyghidra venv. stdin: lines "<addr hex> <word hex>"; stdout: JSON per line
{"text": ..., "pcode": [...] (--pcode), "flow"/"ops" (--dataflow)} or {"error": ...}.
usage: ghidra_decode.py <ldefs> <language id> [--pcode] [--dataflow]
"""
import json, os, sys
import pyghidra

GHIDRA = os.environ.get("GHIDRA_INSTALL_DIR", os.path.expanduser("~/Ghidra/ghidra_12.1.4_PUBLIC"))

def load_language(ldefs, lang_id):
    from java.io import File
    from generic.jar import ResourceFile
    from ghidra.app.plugin.processors.sleigh import SleighLanguageProvider
    from ghidra.program.model.lang import LanguageID, CompilerSpecID
    ctor = SleighLanguageProvider.class_.getDeclaredConstructor(ResourceFile.class_)
    ctor.setAccessible(True)  # package-private; avoids installing the processor into Ghidra
    lang = ctor.newInstance(ResourceFile(File(ldefs))).getLanguage(LanguageID(lang_id))
    # Importers also load the compiler spec; decoding alone misses stale-register failures.
    lang.getCompilerSpecByID(CompilerSpecID('default'))
    return lang

def parse(lang, addr, word):
    """The instruction encoded by `word` at address `addr`, outside any program."""
    from ghidra.app.util import PseudoInstruction
    from ghidra.program.model.lang import ProcessorContextImpl
    from ghidra.program.model.mem import ByteMemBufferImpl
    buf, ctx = ByteMemBufferImpl(addr, word, False), ProcessorContextImpl(lang)
    return PseudoInstruction(addr, lang.parse(buf, ctx, False), buf, ctx)

def describe(lang, addr, word, pcode=False, dataflow=False):
    """The instruction encoded by `word` at `addr` (an Address): its text, and optionally its p-code
    and the dataflow of its ops; {"error": ...} if it doesn't decode."""
    try:
        ins = parse(lang, addr, word)
        out = {"text": str(ins)}
        if pcode: out["pcode"] = [str(op) for op in ins.getPcode()]
        if dataflow:
            ops = ins.getPcode()
            flow = ins.getFlowType()
            out["flow"] = {"jump": bool(flow.isJump()), "call": bool(flow.isCall()), "terminal": bool(flow.isTerminal())}
            def node(v):
                return {"space": str(v.getAddress().getAddressSpace().getName()),
                        "offset": int(v.getOffset()), "size": int(v.getSize())}
            out["ops"] = [{"op": str(op.getMnemonic()),
                           "userop": str(lang.getUserDefinedOpName(int(op.getInput(0).getOffset())))
                           if str(op.getMnemonic()) == 'CALLOTHER' else None,
                           "inputs": [node(v) for v in op.getInputs()],
                           "output": node(op.getOutput()) if op.getOutput() else None} for op in ops]
        return out
    except Exception as e:
        return {"error": str(e).splitlines()[0]}

def main(ldefs, lang_id, *opts):
    pyghidra.start(install_dir=GHIDRA)
    lang = load_language(ldefs, lang_id)
    space = lang.getDefaultSpace()
    for line in sys.stdin:
        a, w = line.split()
        print(json.dumps(describe(lang, space.getAddress(int(a, 16)), bytes.fromhex(w), "--pcode" in opts, "--dataflow" in opts)), flush=False)

if __name__ == "__main__":
    main(*sys.argv[1:])
