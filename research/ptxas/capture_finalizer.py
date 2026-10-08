"""Source in rr/GDB to capture the intermediate -> final SASS bridge.

Specific to CUDA 13.0.88 ptxas (addresses in ptxas-finalizer-notes.md).
Use: rr replay TRACE, then `source /absolute/path/capture_finalizer.py`,
then `continue`. JSONL output defaults to /tmp/ptxas_finalizer_capture.jsonl;
set PTXAS_CAPTURE before launching rr to select a different output file.
Does not change inferior memory or execution results.
"""
import json
import os
import struct
import gdb

OUTPUT = os.environ.get("PTXAS_CAPTURE", "/tmp/ptxas_finalizer_capture.jsonl")


def memory(address, size):
    return bytes(gdb.selected_inferior().read_memory(address, size))


def register(name):
    return int(gdb.parse_and_eval("$" + name))


def u64(address):
    return struct.unpack("<Q", memory(address, 8))[0]


def emit(record):
    with open(OUTPUT, "a") as output:
        output.write(json.dumps(record) + "\n")


class Intermediate(gdb.Breakpoint):
    def stop(self):
        context, instruction = register("rdi"), register("rsi")
        first = u64(context + 0x220)
        length = (first & 15) * 16
        emit({"stage": "intermediate_decode", "instruction": hex(instruction),
              "length": length, "namespace": (first >> 4) & 7,
              "opcode": (first >> 8) & 511,
              "keys": [(first >> 17) & 255, (first >> 25) & 127],
              "bytes": memory(context + 0x220, length).hex()})
        return False


class Encoded(gdb.FinishBreakpoint):
    def __init__(self, instruction, scratch):
        super().__init__(gdb.newest_frame(), internal=True)
        self.instruction, self.scratch = instruction, scratch

    def stop(self):
        emit({"stage": "native_encoded", "instruction": hex(self.instruction),
              "bytes": memory(self.scratch, 16).hex()})
        return False


class Native(gdb.Breakpoint):
    def stop(self):
        encoder, instruction = register("rdi"), register("rsi")
        opcode, a, b = struct.unpack("<HBB", memory(instruction + 12, 4))
        table, count = struct.unpack("<QQ", memory(0x232e6e0 + opcode * 16, 16))
        matches = []
        for index in range(count):
            address = table + index * 24
            raw = memory(address, 24)
            if raw[0] == a and raw[1] == b:
                handler, adjustment = struct.unpack("<Qq", raw[8:])
                matches.append({"row": hex(address), "handler": hex(handler),
                                "this_adjustment": adjustment})
        operands = u64(instruction + 32)
        last = struct.unpack("<i", memory(instruction + 40, 4))[0]
        emit({"stage": "native_dispatch", "instruction": hex(instruction),
              "opcode": opcode, "keys": [a, b], "matches": matches,
              "selector_flags": hex(u64(instruction + 0x30)),
              "operands": [memory(operands + i * 32, 32).hex()
                           for i in range(last + 1)]})
        Encoded(instruction, u64(encoder + 40))
        return False


class LowerAdd(gdb.Breakpoint):
    def stop(self):
        instruction = register("rsi")
        count = struct.unpack("<I", memory(instruction + 0x50, 4))[0]
        emit({"stage": "lower_add", "instruction": hex(instruction),
              "opcode_flags": struct.unpack("<I", memory(instruction + 0x48, 4))[0],
              "type": struct.unpack("<I", memory(instruction + 0x4c, 4))[0],
              "operand_count": count,
              "operand_descriptors": memory(instruction + 0x54, count * 8).hex()})
        return False


class ConvertOperation(gdb.Breakpoint):
    def stop(self):
        instruction = register("rsi")
        flags, type_code, count = struct.unpack("<III", memory(instruction + 0x48, 12))
        emit({"stage": "convert_operation", "instruction": hex(instruction),
              "opcode_flags": flags, "type": type_code, "operand_count": count,
              "operand_descriptors": memory(instruction + 0x54, count * 8).hex()})
        return False


class BuiltOperand(gdb.FinishBreakpoint):
    def __init__(self, output, record):
        super().__init__(gdb.newest_frame(), internal=True)
        self.output, self.record = output, record

    def stop(self):
        self.record["payload"] = memory(self.output, 64).hex()
        emit(self.record)
        return False


class BuildOperand(gdb.Breakpoint):
    def __init__(self, address, destination):
        super().__init__(address, internal=True)
        self.destination = destination

    def stop(self):
        output, conversion, instruction = (register(n) for n in ("rdi", "rsi", "rdx"))
        flags = struct.unpack("<I", memory(instruction + 0x48, 4))[0]
        if flags & 0xffffcfff not in (2, 5, 0x6e, 0x70, 0x72, 0x73, 0x75, 0x8b, 0x8c, 0x8d, 0x8f):
            return False
        index = 0 if self.destination else register("rcx")
        descriptor = memory(instruction + 0x54 + index * 8, 8)
        tag, modifiers = struct.unpack("<II", descriptor)
        record = {"stage": "build_target_operand", "index": index,
                  "descriptor": descriptor.hex(), "modifiers": modifiers}
        if (tag >> 28) & 7 == 1:
            context = u64(conversion + 8)
            ir_register = u64(u64(context + 0x58) + (tag & 0xffffff) * 8)
            kind, physical = struct.unpack("<II", memory(ir_register + 0x40, 8))
            record.update({"register_id": tag & 0xffffff, "register_class": kind,
                           "allocated_number": physical})
        BuiltOperand(output, record)
        return False


class WideOperation(gdb.Breakpoint):
    def stop(self):
        instruction = register("rsi")
        flags, type_code, count = struct.unpack("<III", memory(instruction + 0x48, 12))
        if flags & 0xffffcfff == 2 and type_code in (9, 10):
            emit({"stage": "wide_add_input", "instruction": hex(instruction),
                  "opcode_flags": flags, "type": type_code,
                  "operand_descriptors": memory(instruction + 0x54, count * 8).hex()})
        return False


class LowerMultiply(gdb.Breakpoint):
    def stop(self):
        instruction = register("rsi")
        flags, type_code, count = struct.unpack("<III", memory(instruction + 0x48, 12))
        emit({"stage": "lower_multiply", "instruction": hex(instruction),
              "opcode_flags": flags, "type": type_code, "operand_count": count,
              "operand_descriptors": memory(instruction + 0x54, count * 8).hex()})
        return False


class ParsedMultiplyShape(gdb.FinishBreakpoint):
    def __init__(self, output):
        super().__init__(gdb.newest_frame(), internal=True)
        self.output = output

    def stop(self):
        raw = memory(self.output, 0x3c)
        fields = struct.unpack_from("<9i", raw, 0x10)
        emit({"stage": "multiply_shape", "instruction": hex(u64(self.output + 8)),
              "destination_index": fields[0], "source_indices": list(fields[1:3]),
              "addend_index": fields[3], "predicate_class": fields[4],
              "predicate_output_index": fields[5], "predicate_input_index": fields[6],
              "has_addend": raw[0x34], "low_result": raw[0x35],
              "high_result": raw[0x36], "wide_result": raw[0x37],
              "mode_3_flag": raw[0x38]})
        return False


class ParseMultiplyShape(gdb.Breakpoint):
    def stop(self):
        ParsedMultiplyShape(register("rdi"))
        return False


class SixOperand(gdb.Breakpoint):
    def stop(self):
        if register("rdx") & 0xffffcfff != 5:
            return False
        stack = register("rsp")
        addresses = [register("r8"), register("r9")]
        addresses += [u64(stack + 8 + index * 8) for index in range(4)]
        emit({"stage": "emit_carry_operation", "type": register("rcx"),
              "operand_descriptors": [memory(a, 8).hex() for a in addresses]})
        return False


class HighMultiplyLegalization(gdb.Breakpoint):
    def stop(self):
        instruction = register("rsi")
        flags, type_code, count = struct.unpack("<III", memory(instruction + 0x48, 12))
        emit({"stage": "high_multiply_legalization_input", "instruction": hex(instruction),
              "opcode_flags": flags, "type": type_code,
              "operand_descriptors": memory(instruction + 0x54, count * 8).hex()})
        return False


class SevenOperandMultiply(gdb.Breakpoint):
    def stop(self):
        if register("rdx") & 0xffffcfff != 0x70:
            return False
        stack = register("rsp")
        addresses = [register("r8"), register("r9")]
        addresses += [u64(stack + 8 + index * 8) for index in range(5)]
        emit({"stage": "emit_multiply_operation", "type": register("rcx"),
              "operand_descriptors": [memory(a, 8).hex() for a in addresses]})
        return False


Intermediate("*0x1803d50", internal=True)
Native("*0x12c3510", internal=True)
LowerAdd("*0x9daa40", internal=True)
ConvertOperation("*0x9ed2d0", internal=True)
LowerMultiply("*0x9db020", internal=True)
ParseMultiplyShape("*0x7e1dc0", internal=True)
BuildOperand("*0x9d1880", True)
BuildOperand("*0x9d4380", False)
WideOperation("*0xa36360", internal=True)
SixOperand("*0x9331f0", internal=True)
HighMultiplyLegalization("*0x9c5000", internal=True)
SevenOperandMultiply("*0x933100", internal=True)
print("Capturing finalizer records to " + OUTPUT)
