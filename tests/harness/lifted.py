"""Run layer-4 emulator cases through the Ghidra-free semantics (`sass.semantics.lift` + `sass.semantics.evaluate`)."""
import re
from functools import cache
from sass.decode import ir, pydecode
from sass.semantics import evaluate as sem_eval, lift as sem_lift
from sass.semantics.family import family_for
from tests.harness.checks import problems

NAME = re.compile(r"(UR|UP|R|P)(\d+)$")
PCODE_ONLY = ("__native", "__max_ops", "__events")


def applies(case):
    """Whether layer 4a can judge the case: a single instruction of an opcode with a family."""
    r = case.request
    return "word" in r and not r.get("context") and \
        all(NAME.match(n) or n in ("RZ", "PT") for n in list(r.get("registers", {})) + list(r.get("observe", [])))


def state(request):
    s = sem_eval.State()
    for name, value in request.get("registers", {}).items():
        if m := NAME.match(name): s.write(m[1], int(m[2]), 1, int(value))
    for mem in request.get("memory", []):
        data = bytes.fromhex(mem["hex"])
        s.store(mem["space"], mem["address"], 8 * len(data), int.from_bytes(data, "little"))
    return s


def observe(s, names):
    return {n: 0 if n == "RZ" else 1 if n == "PT" else s.read(m[1], int(m[2]), 1) for n in names if (m := NAME.match(n)) or n in ("RZ", "PT")}


@cache
def lift(arch, word, address, fp):
    """Lifted once per word: exhaustive cases share few words (F2FP scaled upconverts: 12288 cases, 48 words)."""
    return sem_lift.lift(arch, word, address, fp)


def run(arch, fp, case):
    """(status, problems) for one case; status native | opaque | illegal."""
    word = bytes.fromhex(case.request["word"])
    try: lifted = lift(arch, word, case.request.get("address", 0x1000), fp)
    except pydecode.NoMatch as e: return "illegal", [f"does not decode: {e}"]
    expected = {k: v for k, v in case.expected.items() if k not in PCODE_ONLY}
    opaque = "__opaque" in expected or str(expected.get("error", "")).startswith(("sass_opaque", "sass_prim"))
    if lifted.status == "primitive": return "primitive", []   # the runtime primitive's values: layer 4
    if lifted.status == "opaque":
        if primitive_fallback(arch, lifted.cls): return "primitive", []   # the runtime primitive's values: layer 4
        return "opaque", [] if opaque else [f"unmodelled ({lifted.reason}), expected values"]
    if opaque: return "native", ["modelled, expected an opaque fallback"]
    if "error" in expected: return "native", []  # runtime errors (budgets, events) are emulator matters
    try: out = sem_eval.execute(lifted, state(case.request))
    except sem_eval.Unknown as e: return "native", [str(e)]
    memory = [dict(hex=bytes(out.memory.get(m["space"], {}).get(m["address"] + j, 0) for j in range(m["size"])).hex())
              for m in case.request.get("observe_memory", [])]
    return "native", problems(expected, dict(registers=observe(out, case.request.get("observe", [])), counter=out.pc, memory=memory))


def primitive_fallback(arch, cls):
    """Whether the class's unmodelled encodings fall back to a runtime primitive (FADD, F2I, ...)."""
    fam = family_for(next(k for k in ir.load(arch).classes if k.name == cls))
    return getattr(fam, "fallback", "opaque") == "prim"


def has_family(md, cls):
    return family_for(next(k for k in md.classes if k.name == cls)) is not None
