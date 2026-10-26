"""Reachability claims for each instruction and both complete example runs."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from sass.formal import k
from sass.formal.k import tools


@dataclass(frozen=True)
class Boundary:
    predicate: int
    step: int
    pc: int
    value: int

    def registers(self):
        return k.kmap([('location("R", 0)', self.value),
                       ('location("P", 0)', self.predicate)])


def boundaries(worked, predicate):
    _, rows = worked.trace(predicate)
    first = Boundary(predicate, 0, rows[0][1], rows[0][2])
    return [first, *[Boundary(predicate, step, pc, value)
                    for step, _, _, value, pc in rows]]


def claim(program, before, after, budget, label, *, halt=False):
    """Keep architectural state exact; quantify bookkeeping reset by dispatch."""
    return f"""
  claim <sass>
    <k> next => {'.K' if halt else 'next'} </k>
    <program> {program} </program>
    <pc> {before.pc} => {after.pc} </pc>
    <nextPc> _NEXT:Int => {after.pc} </nextPc>
    <regs> {before.registers()} => {after.registers()} </regs>
    <memory> .Map </memory>
    <params> .Map </params>
    <responses> .Map </responses>
    <environment> .Map </environment>
    <events> .List </events>
    <memo> _CACHE:Map => ?_CACHE:Map </memo>
    <pendingRegs> _WR:Map => ?_WR:Map </pendingRegs>
    <pendingMemory> _WM:Map => ?_WM:Map </pendingMemory>
    <step> {before.step} => {after.step} </step>
    <remaining> {budget - before.step} => {budget - after.step} </remaining>
    <status> "running" => {'"halted"' if halt else '"running"'} </status>
  </sass>
  [label({label})]
"""


def specification(worked, budget):
    program = k.kmap([(instruction.addr, k.Encoder().instruction(instruction))
                     for instruction in worked.instructions])
    claims = []
    for predicate in (0, 1):
        states = boundaries(worked, predicate)
        for before, after in zip(states, states[1:]):
            claims.append(claim(program, before, after, budget,
                                f"p{predicate}-step-{after.step}"))
        # At PC zero, nextPc is already zero. Halting changes only status and k.
        end = states[-1]
        claims.append(f"""
  claim <k> next => .K </k> <pc> 0 </pc>
        <regs> {end.registers()} </regs>
        <step> {end.step} </step>
        <status> "running" => "halted" </status>
    [label(p{predicate}-halt)]
""")
        claims.append(claim(program, states[0], end, budget,
                            f"p{predicate}-complete", halt=True))
    return ('requires "sass.md"\nmodule SASS-EXECUTION-PROOF\n  imports SASS\n'
            + "\n".join(claims) + "\nendmodule\n")


def publication(worked, directory, budget, *, prove=False):
    source = specification(worked, budget)
    path = directory / "claims.k"
    path.write_text(source)
    digest = hashlib.sha256(source.encode())
    for definition in k.SOURCES:
        digest.update(definition.read_bytes())
    record = dict(k_version=k.VERSION, fingerprint=digest.hexdigest(),
                  status="not checked", output="",
                  claim_count=sum(len(boundaries(worked, p)) + 1 for p in (0, 1)),
                  boundaries={str(p): [asdict(state) for state in boundaries(worked, p)]
                              for p in (0, 1)})
    log = directory / "proof.log"
    log.unlink(missing_ok=True)
    record_path = directory / "proof.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    if prove:
        result = tools.prove(path, "SASS-EXECUTION-PROOF", timeout=180)
        log.write_text(result.stdout + result.stderr)
        if "#Top" not in result.stdout:
            raise tools.ToolError("example reachability proof did not return #Top")
        record["status"] = "proved"
        record["output"] = result.stdout
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def complete_claims(source):
    """Quote the two complete-run claims from the actual generated specification."""
    matches = re.finditer(r"^  claim\b.*?^\s*\[label\(([^)\n]+)\)\]",
                          source, re.MULTILINE | re.DOTALL)
    selected = [match[0] for match in matches
                if match[1] in ("p0-complete", "p1-complete")]
    if len(selected) != 2:
        raise ValueError("expected both complete-run K claims")
    return "\n\n".join(selected)
