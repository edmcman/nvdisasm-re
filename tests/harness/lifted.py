"""Run the emulator cases through the Ghidra-free semantics (`sass.semantics.lifter` + `sass.semantics.evaluate`)."""
import re
from functools import cache, lru_cache
from sass.decode import ir, pydecode
from sass.emulate import reference
from sass.semantics import evaluate, lifter
from sass.semantics.family import family_for
from sass.semantics.runtime import Runtime, UnsupportedSemantics
from tests.harness.checks import problems

NAME = re.compile(r"(UR|UP|R|P)(\d+)$")
SPECIAL = re.compile(r"SR_[\w.]+$")   # a special register seeded by name (SR_SWINLO)
PCODE_ONLY = ("__native", "__max_ops", "__atomic")


def applies(case):
    """Whether the lifted semantics can judge the case: a whole kernel, or a single instruction of an opcode with a family."""
    r = case.request
    return "kernel" in r or "word" in r and \
        all(NAME.match(n) or n in ("RZ", "PT") for n in r.get("observe", [])) and \
        all(NAME.match(n) or SPECIAL.match(n) or n in ("RZ", "PT") for n in r.get("registers", {}))


def state(arch, request):
    s = evaluate.State()
    for name, value in request.get("registers", {}).items():
        if m := NAME.match(name): s.write(m[1], int(m[2]), 1, int(value))
        elif SPECIAL.match(name): s.write("SR", ir.load(arch).enums["SpecialRegister"][name], 1, int(value))
    for mem in request.get("memory", []):
        data = bytes.fromhex(mem["hex"])
        s.store(mem["space"], mem["address"], 8 * len(data), int.from_bytes(data, "little"))
    return s


def observe(s, names):
    return {n: 0 if n == "RZ" else 1 if n == "PT" else s.read(m[1], int(m[2]), 1) for n in names if (m := NAME.match(n)) or n in ("RZ", "PT")}


@cache
def modelled(arch):
    first = {}
    for k in ir.load(arch).classes: first.setdefault(k.name, k)
    return {name for name, k in first.items() if family_for(k)}


def judged(job):
    """(item, case) for each case of the job's shard (`tests.cases.stream`) that the lifted semantics can judge."""
    from tests.cases import stream
    arch, fp, family, *_ = job
    for case in stream(*job):
        if ("kernel" in case.request or case.cls in modelled(arch)) and applies(case): yield (arch, fp, family, case.cls), case


@lru_cache(maxsize=4096)
def lift(arch, word, address, fp):
    """Lifted once per recent word: sweep cases share few words (F2FP scaled upconverts: 12288 cases,
    48 words); bounded, since a worker lifts hundreds of thousands of words over a session."""
    return lifter.lift(arch, word, address, fp)


def evaluated(arch, lifted, case, runtime):
    return evaluate.execute(lifted, state(arch, case.request), runtime=runtime)


def run(arch, fp, case, execute=evaluated):
    """(status, problems) for one case; status native | primitive | opaque | illegal | kernel.
    `execute(arch, lifted, case, runtime)` gives the state after the instruction (default: the
    Python evaluator; tests/test_emulate_cases.py passes the native fragments)."""
    if "kernel" in case.request: return "kernel", problems(case.expected, reference.run_kernel(case.request, arch, fp))
    word = bytes.fromhex(case.request["word"])
    try: lifted = lift(arch, word, case.request.get("address", 0x1000), fp)
    except pydecode.NoMatch as e:
        if "illegal" not in case.expected: return "illegal", [f"does not decode: {e}"]
        return "illegal", [] if all(part in str(e) for part in case.expected["illegal"]) else [f"rejected for another reason: {e}"]
    if "illegal" in case.expected: return lifted.status, [f"decoded an md-illegal encoding: {lifted.text}"]
    expected = {k: v for k, v in case.expected.items() if k not in PCODE_ONLY}
    opaque = "__opaque" in expected or str(expected.get("error", "")).startswith("sass_opaque")
    if lifted.status == "opaque": return "opaque", [] if opaque else [f"unmodelled ({lifted.reason}), expected values"]
    if opaque: return lifted.status, ["modelled, expected an opaque fallback"]
    runtime = Runtime(int(arch[2:]), context=case.request.get("context"))
    try: out = execute(arch, lifted, case, runtime)
    except UnsupportedSemantics as e: return lifted.status, problems(expected, dict(error=str(e)))
    except evaluate.Unknown as e: return lifted.status, [str(e)]
    if "error" in expected: return lifted.status, []  # kernel budgets are emulator matters
    memory = [dict(hex=bytes(out.memory.get(m["space"], {}).get(m["address"] + j, 0) for j in range(m["size"])).hex())
              for m in case.request.get("observe_memory", [])]
    found = problems(expected, dict(registers=observe(out, case.request.get("observe", [])), counter=out.pc, memory=memory, events=runtime.events))
    return lifted.status, found + (["not atomic"] if "__atomic" in case.expected and lifted.atomic is None else [])
