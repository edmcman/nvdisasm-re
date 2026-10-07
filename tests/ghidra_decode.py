"""Decode instruction words with a SLEIGH language straight from its .ldefs (no project, no install).

Runs under Ghidra's pyghidra venv. stdin: lines "<addr hex> <word hex>"; stdout: JSON per line
{"text": ..., "pcode": [...]} or {"error": ...}.
usage: ghidra_decode.py <ldefs> <language id> [--pcode]
"""
import json, os, sys
import pyghidra

GHIDRA = os.environ.get("GHIDRA_INSTALL_DIR", os.path.expanduser("~/Ghidra/ghidra_12.1.4_PUBLIC"))

def load_language(ldefs, lang_id):
    from java.io import File
    from generic.jar import ResourceFile
    from ghidra.app.plugin.processors.sleigh import SleighLanguageProvider
    from ghidra.program.model.lang import LanguageID
    ctor = SleighLanguageProvider.class_.getDeclaredConstructor(ResourceFile.class_)
    ctor.setAccessible(True)  # package-private; avoids installing the processor into Ghidra
    return ctor.newInstance(ResourceFile(File(ldefs))).getLanguage(LanguageID(lang_id))

def main(ldefs, lang_id, *opts):
    pyghidra.start(install_dir=GHIDRA)
    from ghidra.app.util import PseudoInstruction
    from ghidra.program.model.lang import ProcessorContextImpl
    from ghidra.program.model.mem import ByteMemBufferImpl
    lang = load_language(ldefs, lang_id)
    space, ctx = lang.getDefaultSpace(), ProcessorContextImpl(lang)
    for line in sys.stdin:
        a, w = line.split()
        addr = space.getAddress(int(a, 16))
        try:
            buf = ByteMemBufferImpl(addr, bytes.fromhex(w), False)
            ins = PseudoInstruction(addr, lang.parse(buf, ctx, False), buf, ctx)
            out = {"text": str(ins)}
            if "--pcode" in opts: out["pcode"] = [str(op) for op in ins.getPcode()]
        except Exception as e:
            out = {"error": str(e).splitlines()[0]}
        print(json.dumps(out), flush=False)

if __name__ == "__main__":
    main(*sys.argv[1:])
