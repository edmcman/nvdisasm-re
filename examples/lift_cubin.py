"""Lift every instruction of a cubin: python examples/lift_cubin.py kernel.cubin"""
import sys
from pathlib import Path
from sass.decode import cubin, pydecode
from sass.semantics import lifter

data = Path(sys.argv[1]).read_bytes()
arch = cubin.elf_arch(data)
for name, body in cubin.text_sections(data).items():
    print(arch, name)
    for addr, word in enumerate(cubin.words(body)):
        addr *= 16
        try: sem = lifter.lift(arch, word, addr)
        except pydecode.NoMatch as e: print(f"  {addr:#06x} undecodable: {e}"); continue
        print(f"  {addr:#06x} {sem.text:40} {sem.status} {sem.reason}")
        for loc, v in sem.writes: print(f"      {loc} <- {v}")
        for s in sem.stores: print(f"      {s.space}[{s.address}] <- {s.value}")
        if sem.flow: print(f"      flow {sem.flow.kind} -> {sem.flow.target}")
