"""Run a lifted instruction on concrete registers: R0 = R1 * R2 + R3 = 1.5 * 2.0 + 0.25."""
import struct
from sass.assemble import assemble_instruction
from sass.semantics import evaluate, lifter

def bits(x): return struct.unpack("<I", struct.pack("<f", x))[0]
def value(b): return struct.unpack("<f", struct.pack("<I", b))[0]

sem = lifter.lift("SM89", assemble_instruction("SM89", "FFMA R0, R1, R2, R3"), addr=0x100)
state = evaluate.State()
for r, x in (1, 1.5), (2, 2.0), (3, 0.25): state.write("R", r, 1, bits(x))
after = evaluate.execute(sem, state)
print(sem.text, "->", value(after.read("R", 0, 1)), f"next pc {after.pc:#x}")
