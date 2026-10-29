"""Read-only GDB capture of CUDA 13.0.88's legacy half emission path.

This traces compiler processes, not GPU execution. Set SM75_HALF_CAPTURE to
a fresh JSONL path and source this file in batch GDB before `run`.
The addresses are for the non-PIE ptxas build pinned below.
"""
import hashlib
import json
import os
import struct

import gdb


EXPECTED_SHA256 = "daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2"
binary = gdb.current_progspace().filename
with open(binary, "rb") as stream:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    if digest.hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("capture addresses require CUDA 13.0.88's pinned ptxas")
output = os.environ["SM75_HALF_CAPTURE"]
if os.path.exists(output):
    raise RuntimeError("choose a fresh capture path; existing records are preserved")


def raw(address, size):
    return bytes(gdb.selected_inferior().read_memory(address, size))


def reg(name):
    return int(gdb.parse_and_eval("$" + name))


def pointer(address):
    return struct.unpack("<Q", raw(address, 8))[0]


def record(row):
    with open(output, "a") as stream:
        stream.write(json.dumps(row) + "\n")


def instruction(address):
    flags, kind, count = struct.unpack("<III", raw(address + 0x48, 12))
    return dict(instruction=hex(address), opcode=flags & 0xffffcfff,
                flags=flags, type=kind, operands=raw(address + 0x54, count * 8).hex())


class HalfOperandEligibilityReturned(gdb.FinishBreakpoint):
    def __init__(self, row):
        super().__init__(gdb.newest_frame(), internal=True)
        self.row = row

    def stop(self):
        record(dict(self.row, stage="operand_eligibility_returned", result=reg("rax") & 255))
        return False


class HalfOperandEligibility(gdb.Breakpoint):
    def stop(self):
        address = reg("rsi")
        row = instruction(address)
        if row["opcode"] not in (0x10e, 0x117, 0x11a, 0x11d):
            return False
        context, index = pointer(reg("rdi")), reg("rdx")
        row.update(stage="operand_eligibility", operand=index,
                   properties=struct.unpack("<I", raw(pointer(context + 0x308) + row["opcode"] * 4, 4))[0],
                   context_flags=raw(context + 0x574, 1).hex(),
                   paired_operand=raw(address + 0x5c + index * 8, 4).hex())
        record(row)
        HalfOperandEligibilityReturned(row)
        return False


class ConversionRemoved(gdb.Breakpoint):
    def stop(self):
        address = reg("rsi")
        if struct.unpack("<I", raw(address + 0x48, 4))[0] & 0xffffcfff == 0x3c:
            record(dict(stage="conversion_removed", **instruction(address),
                        backtrace=gdb.execute("bt 10", to_string=True)))
        return False


class ConversionEligibility(gdb.Breakpoint):
    def stop(self):
        address = reg("rsi")
        flags, kind, count = struct.unpack("<III", raw(address + 0x48, 12))
        if flags & 0xffffcfff != 0x3c or kind != 7:
            return False
        context = pointer(reg("rdi"))
        nodes = []
        for index in range(count):
            value, modifier = struct.unpack("<II", raw(address + 0x54 + index * 8, 8))
            if value >> 28 & 7 == 1:
                node = pointer(pointer(context + 0x58) + (value & 0xffffff) * 8)
                nodes.append(dict(index=index, register_id=value & 0xffffff,
                                  modifier=modifier, metadata=raw(node + 0x30, 0x28).hex()))
        record(dict(stage="conversion_eligibility", **instruction(address), registers=nodes))
        return False


class EmissionFinished(gdb.FinishBreakpoint):
    def __init__(self, encoder, row):
        super().__init__(gdb.newest_frame(), internal=True)
        self.encoder, self.row = encoder, row

    def stop(self):
        self.row.update(stage="half_encoded", word=raw(pointer(self.encoder + 0xa0), 16).hex(),
                        fields=raw(pointer(self.encoder + 0xd0) + 0x78, 0x60).hex(),
                        final_modes=list(struct.unpack("<8I", raw(self.encoder + 0x20, 32))))
        record(self.row)
        return False


class HalfEmitter(gdb.Breakpoint):
    def __init__(self, address, name):
        super().__init__(address, internal=True)
        self.name = name

    def stop(self):
        encoder, address = reg("rdi"), reg("rsi")
        row = dict(stage="half_emission", handler=self.name, **instruction(address),
                   vtable=hex(pointer(encoder)),
                   modes=list(struct.unpack("<8I", raw(encoder + 0x20, 32))),
                   backtrace=gdb.execute("bt 6", to_string=True))
        record(row)
        EmissionFinished(encoder, row)
        return False


class ConversionTypeChanged(gdb.Breakpoint):
    def __init__(self, address):
        super().__init__(f"*(unsigned int *){address + 0x4c:#x}",
                         type=gdb.BP_WATCHPOINT, wp_class=gdb.WP_WRITE, internal=True)
        self.address = address

    def stop(self):
        # A later allocator reuse is not a conversion rewrite.
        row = instruction(self.address)
        if row["opcode"] == 0x3c:
            record(dict(stage="conversion_type_changed", **row,
                        backtrace=gdb.execute("bt 12", to_string=True)))
        return False


class HalfEligibilityChanged(gdb.Breakpoint):
    def __init__(self, address, register_id):
        super().__init__(f"*(unsigned int *){address + 0x4c:#x}",
                         type=gdb.BP_WATCHPOINT, wp_class=gdb.WP_WRITE, internal=True)
        self.address, self.register_id = address, register_id
        self.previous = struct.unpack("<I", raw(address + 0x4c, 4))[0]

    def stop(self):
        value = struct.unpack("<I", raw(self.address + 0x4c, 4))[0]
        if (self.previous ^ value) & 1:
            record(dict(stage="half_eligibility_changed", register_id=self.register_id,
                        previous=self.previous, value=value,
                        metadata=raw(self.address + 0x30, 0x28).hex(),
                        backtrace=gdb.execute("bt 12", to_string=True)))
        self.previous = value
        return False


class WatchFirstHalfConversion(gdb.Breakpoint):
    watching = False

    def stop(self):
        if self.watching:
            return False
        address = pointer(reg("rsi") + 0x110)
        while address:
            flags, kind = struct.unpack("<II", raw(address + 0x48, 8))
            if flags & 0xffffcfff == 0x3c and kind == 7:
                record(dict(stage="watch_half_conversion", **instruction(address)))
                ConversionTypeChanged(address)
                register_id = struct.unpack("<I", raw(address + 0x54, 4))[0] & 0xffffff
                node = pointer(pointer(reg("rsi") + 0x58) + register_id * 8)
                HalfEligibilityChanged(node, register_id)
                self.watching = True
                break
            address = pointer(address + 8)
        return False


ConversionRemoved("*0x9253c0", internal=True)
ConversionEligibility("*0x821370", internal=True)
HalfEmitter("*0xc529c0", "hmul2_fields")
HalfEmitter("*0xc52190", "hfma2_fields")
# HADD2 has another field builder behind its selector; capturing the whole
# selector also preserves its input shape and final encoded word.
HalfEmitter("*0xa3ed80", "hadd2_selector")
if os.environ.get("SM75_HALF_TRACE_RULES"):
    HalfOperandEligibility("*0x820960", internal=True)
if os.environ.get("SM75_HALF_WATCH_CVT"):
    # Use a single-entry PTX input: this consumes two hardware watchpoints.
    WatchFirstHalfConversion("*0xc6508d", internal=True)
record(dict(stage="identity", binary=binary, sha256=EXPECTED_SHA256,
            kind="compiler breakpoints; no GPU execution"))
