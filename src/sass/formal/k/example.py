"""The publication's lifted program, generated through the public APIs."""
from dataclasses import dataclass
import json

from sass.assemble import assemble_raw
from sass.formal import k
from sass.formal.k import execution
from sass.semantics import evaluate, lifter

ARCH = "SM89"
BASE = 0x100000
LIMIT = 8
SOURCE = """IADD3 R0, R0, 1, RZ;
@P0 BRA done;
IADD3 R0, R0, 2, RZ;
done:
EXIT;
"""


@dataclass
class Example:
    code: bytes
    instructions: list
    exported: k.Export

    def initial(self, predicate):
        return evaluate.State(regs={("R", 0): 0xffffffff, ("P", 0): predicate}, pc=BASE)

    def input(self, predicate):
        # Compile the exported module to give this short input its program symbol.
        return k.input_term(self.exported, self.initial(predicate), limit=LIMIT).replace(
            self.exported.program, "exportedProgram()", 1)

    def trace(self, predicate):
        """Record the reference evaluator's instruction-boundary states."""
        program = {instruction.addr: instruction for instruction in self.instructions}
        state, rows = self.initial(predicate), []
        while state.pc:
            if len(rows) >= LIMIT:
                raise RuntimeError("worked example exceeded its instruction budget")
            pc, before = state.pc, state.regs["R", 0]
            state = evaluate.execute(program[pc], state)
            rows.append((len(rows) + 1, pc, before, state.regs["R", 0], state.pc))
        return state, rows

    def diagram(self):
        """Typeset actual boundary states, sharing the two paths' visible prefix."""
        _, false = self.trace(0)
        _, true = self.trace(1)

        def node(name, x, y, pc, value, style, note=""):
            return (
                rf"\node[state,{style}] ({name}) at ({x},{y}) "
                + r"{\texttt{PC = " + f"{pc:#x}" + r"}\\"
                + r"\texttt{R0 = " + f"{value:#010x}" + r"}"
                + (r"\\" + note if note else "") + "};"
            )

        return "\n".join([
            r"\begin{center}",
            r"\begin{tikzpicture}[>=stealth,thick,font=\small,",
            r"state/.style={draw,rounded corners,align=left,minimum width=43mm,inner sep=5pt},",
            r"common/.style={draw=blue!65!black,fill=blue!5},",
            r"fallthrough/.style={draw=teal!65!black,fill=teal!5},",
            r"taken/.style={draw=orange!75!black,fill=orange!8}]",
            node("entry", 0, 0, BASE, 0xffffffff, "common", r"$P0=p$"),
            node("shared", 0, -1.8, false[0][4], false[0][3], "common"),
            node("fall", -3.3, -4.5, false[1][4], false[1][3], "fallthrough"),
            node("exitfalse", -3.3, -6.7, false[2][4], false[2][3], "fallthrough"),
            node("haltfalse", -3.3, -8.9, false[3][4], false[3][3], "fallthrough,double",
                 f"halted; {len(false)} instructions"),
            node("exittrue", 3.3, -4.5, true[1][4], true[1][3], "taken"),
            node("halttrue", 3.3, -8.9, true[2][4], true[2][3], "taken,double",
                 f"halted; {len(true)} instructions"),
            r"\draw[->] (entry) -- node[right] {add 1, wrap at $2^{32}$} (shared);",
            r"\draw[teal!65!black] (shared.south) -- (0,-3.15);",
            r"\draw[->,teal!65!black] (0,-3.15) -- node[above,align=center]"
            r" {$P0=0$\\skip BRA} (-3.3,-3.15) -- (fall.north);",
            r"\draw[->,orange!75!black] (0,-3.15) -- node[above,align=center]"
            r" {$P0=1$\\take BRA} (3.3,-3.15) -- (exittrue.north);",
            r"\draw[->,teal!65!black] (fall) -- node[right] {add 2} (exitfalse);",
            r"\draw[->,teal!65!black] (exitfalse) -- node[right] {EXIT} (haltfalse);",
            r"\draw[->,orange!75!black] (exittrue) -- node[right] {EXIT} (halttrue);",
            r"\end{tikzpicture}",
            r"\end{center}",
        ])

    def publication(self, directory, *, prove=False):
        """Write reproducible example inputs and fill the document's worked section."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "program.sass").write_text(SOURCE)
        (directory / "program.bin").write_bytes(self.code)
        (directory / "program.k").write_text(self.exported.source)
        (directory / "program.json").write_text(json.dumps(self.exported.manifest(), indent=2) + "\n")
        for predicate in (0, 1):
            (directory / f"input-p0-{predicate}.sass").write_text(self.input(predicate) + "\n")
        proof = execution.publication(self, directory, LIMIT, prove=prove)
        listing = []
        for instruction in self.instructions:
            listing.append(f"{instruction.addr:#x}: {instruction.text}")
            listing.append("  guard: " + (f"{instruction.guard}:{instruction.guard.w}"
                                         if instruction.guard is not None else "always"))
            for location, value in instruction.writes:
                listing.append(f"  write: {location}:{location.w} <- {value}")
            if instruction.flow:
                listing.append(f"  flow: {instruction.flow.kind} {instruction.flow.target}")
            else:
                listing.append(f"  flow: fall through to {instruction.addr + 16:#x}")
        traces = []
        for predicate in (0, 1):
            state, rows = self.trace(predicate)
            traces += [
                f"### P0 = {predicate}\n",
                "| Step | PC | R0 before | R0 after | Next PC |",
                "| ---: | :--- | :--- | :--- | :--- |",
                *[f"| {step} | `{pc:#x}` | `{before:#010x}` | `{after:#010x}` | `{next_pc:#x}` |"
                  for step, pc, before, after, next_pc in rows],
                f"\nFinal R0 is `{state.regs['R', 0]:#010x}` after {len(rows)} instructions.\n",
            ]
        # Line breaks between map entries improve the listing without altering terms.
        source = self.exported.source
        for instruction in self.instructions[1:]:
            source = source.replace(f" ({instruction.addr} |->", f"\n    ({instruction.addr} |->")
        replacements = {
            "{{assembly}}": SOURCE.rstrip(),
            "{{lifted}}": "\n".join(listing),
            "{{exported}}": source.rstrip(),
            "{{input}}": self.input(0),
            "{{traces}}": "\n".join(traces),
            "{{diagram}}": self.diagram(),
            "{{derivation}}": execution.derivation(self),
            "{{proof_status}}": proof["status"],
        }
        text = k.DEFINITION.with_name("worked-example.md").read_text()
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        return text


def make():
    code = assemble_raw(ARCH, SOURCE, base=BASE)
    instructions = [lifter.lift(ARCH, code[offset:offset + 16], BASE + offset)
                    for offset in range(0, len(code), 16)]
    if any(instruction.status != "native" for instruction in instructions):
        raise RuntimeError("worked example requires native lifted semantics")
    exported = k.export_program(ARCH, code, base=BASE)
    if exported.contracts:
        raise RuntimeError("worked example unexpectedly requires runtime contracts")
    return Example(code, instructions, exported)
