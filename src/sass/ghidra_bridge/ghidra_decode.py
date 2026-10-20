"""Decode instruction words with a SLEIGH language straight from its .ldefs (no project, no install).

Runs under Ghidra's pyghidra venv. stdin: lines "<addr hex> <word hex>"; stdout: JSON per line
{"text": ..., "pcode": [...] (--pcode), "flow"/"ops" (--dataflow)} or {"error": ...}.
usage: ghidra_decode.py <ldefs> <language id> [--pcode] [--dataflow]
"""
import json, os, sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parents[2])]
import pyghidra

GHIDRA = os.environ.get("GHIDRA_INSTALL_DIR", os.path.expanduser("~/Ghidra/ghidra_12.1.4_PUBLIC"))

def java_class(name):
    """sass.`name` from this directory's .java sources, compiled against this Ghidra into the cache."""
    import hashlib, shutil, subprocess, tempfile, jpype
    from sass.paths import CACHE
    from java.io import File
    from java.net import URL, URLClassLoader
    from java.lang import Class, System
    from ghidra.app.plugin.processors.sleigh import SleighLanguage
    sources = sorted(Path(__file__).parent.glob("*.java"))
    release = str(System.getProperty("java.specification.version"))  # PATH's javac may be newer than Ghidra's JVM
    out = CACHE / "java" / hashlib.sha256(b"".join(f.read_bytes() for f in sources) + GHIDRA.encode() + release.encode()).hexdigest()[:16]
    if not out.exists():
        jars = ":".join(map(str, Path(GHIDRA, "Ghidra").rglob("*.jar")))
        tmp = tempfile.mkdtemp(dir=out.parent.mkdir(parents=True, exist_ok=True) or out.parent)
        subprocess.run(["javac", "--release", release, "-proc:none", "-nowarn", "-cp", jars, "-d", tmp, *map(str, sources)], check=True)
        try: os.rename(tmp, out)
        except OSError: shutil.rmtree(tmp)  # a concurrent process compiled it first
    loader = URLClassLoader(jpype.JArray(URL)([File(str(out)).toURI().toURL()]), SleighLanguage.class_.getClassLoader())
    return jpype.JClass(Class.forName(f"sass.{name}", True, loader))

def load_language(ldefs, lang_id):
    from java.io import File
    from generic.jar import ResourceFile
    from ghidra.app.plugin.processors.sleigh import SleighLanguage, SleighLanguageProvider
    from ghidra.program.model.lang import LanguageID, CompilerSpecID
    ctor = SleighLanguageProvider.class_.getDeclaredConstructor(ResourceFile.class_)
    ctor.setAccessible(True)  # package-private; avoids installing the processor into Ghidra
    lang = ctor.newInstance(ResourceFile(File(ldefs))).getLanguage(LanguageID(lang_id))
    prototypes = SleighLanguage.class_.getDeclaredField("instructProtoMap")
    prototypes.setAccessible(True)
    prototypes.set(lang, java_class("ExactPrototypes")())
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
