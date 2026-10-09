"""Log parsed PTX instructions before lowering (CUDA 13.0.88 ptxas only)."""
import json
import os
import gdb

inf = gdb.selected_inferior()
out = open(os.environ["PTX_IR_OUT"], "w")


def value(address, size=8):
    return int.from_bytes(inf.read_memory(address, size).tobytes(), "little")


class Instruction(gdb.Breakpoint):
    def stop(self):
        instruction = int(gdb.parse_and_eval("$rsi"))
        descriptor = value(instruction + 0x20)
        name = inf.read_memory(value(descriptor), 64).tobytes().split(b"\0")[0].decode()
        count = value(descriptor + 0xE8, 4)
        if count > 32:
            raise ValueError(f"unexpected operand count {count}; check ptxas build")
        operands = value(instruction + 0x60)
        expressions = []
        for index in range(count):
            expr = value(operands + index * 8)
            expressions.append({"tag": value(expr, 1) & 0x3F,
                                "raw": inf.read_memory(expr, 0x20).tobytes().hex()})
        print(json.dumps({"id": value(instruction), "opcode": name,
                          "opcode_id": value(descriptor + 8, 4),
                          "modifier_bytes": inf.read_memory(instruction + 0x30, 0x30).tobytes().hex(),
                          "type_record": inf.read_memory(value(instruction + 0x28), 0x10).tobytes().hex(),
                          "operands": expressions}), file=out, flush=True)
        return False


Instruction("*0x62e890")
gdb.execute("run")
out.close()
