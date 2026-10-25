"""Assemble one instruction, decode it back to nvdisasm's text and lift it:
python examples/assemble_and_lift.py SM89 "FFMA R0, R1, R2, R3" """
import sys
from sass.assemble import assemble_instruction
from sass.decode import pydecode
from sass.semantics import lifter

arch, text = sys.argv[1:] if len(sys.argv) > 1 else ("SM89", "FFMA R0, R1, R2, R3")
word = assemble_instruction(arch, text)
d = pydecode.decode(arch, word)
print(word.hex(), d.text, d.klass.name)
sem = lifter.lift(arch, word)
print(sem.status, sem.reason)
for loc, v in sem.writes: print(f"  {loc} <- {v}")
