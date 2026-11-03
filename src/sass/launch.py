"""Kernel launch state shared by the emulator's whole-kernel requests and the GPU harness: the
launch constants' bank-0 layout and the generic memory windows."""
import struct


# Launch constants in bank 0: (blockDim.xyz, local stack base, shared window, local window) offsets, from
# nvcc 13.0 probe kernels reading each value (2026-10-15; docs/testing.md). SM90+ build the generic shared
# base from SR_SWINHI alone (None). SM107 has no nvcc target to check.
LAUNCH_LAYOUT = {**dict.fromkeys(("SM75", "SM80", "SM86", "SM89"), (0x0, 0x28, 0x18, 0x20)), "SM90": (0x0, 0x28, None, 0x20),
                 **dict.fromkeys(("SM100", "SM101", "SM103", "SM120"), (0x360, 0x37c, None, 0x2f8))}
# The emulated generic windows (base, size): 4 GiB-aligned like SM90+'s shared base, 16 MiB like SM89's.
WINDOWS = dict(SWIN=(0x7f00_00000000, 1 << 24), LWIN=(0x7e00_00000000, 1 << 24))


GLOBAL_DESCRIPTOR = {'SM89': 0x118, 'SM90': 0x208}


def cx_layout(arch):
    """(size-field bit, address-unit shift) for a bindless constant header."""
    return (51, 6) if arch == 'SM90' else (45, 4)


def window_registers():
    """The SR_*WIN* registers describing WINDOWS, for every thread."""
    return {f"SR_{w}{part}": v for w, (base, size) in WINDOWS.items()
            for part, v in (("LO", base & 0xffffffff), ("HI", base >> 32), ("SZ", size))}


def serialize_arguments(sizes, arguments):
    sizes, arguments = tuple(sizes), tuple(arguments)
    if len(arguments) < len(sizes):
        raise ValueError(f"argument {len(arguments)} ({sizes[len(arguments)]} bytes): missing value")
    if len(arguments) > len(sizes):
        raise ValueError(f"argument {len(sizes)} (no declared size): extra value")
    result = []
    for ordinal, (size, value) in enumerate(zip(sizes, arguments)):
        label = f"argument {ordinal} ({size} bytes)"
        match value:
            case int():
                if value < 0 or value.bit_length() > size * 8:
                    raise ValueError(f"{label}: unsigned integer out of range")
                payload = value.to_bytes(size, "little")
            case bytes() | bytearray() | memoryview():
                payload = bytes(value)
                if len(payload) != size:
                    raise ValueError(f"{label}: payload has {len(payload)} bytes")
            case _:
                raise TypeError(f"{label}: unsupported value type {type(value).__name__}")
        result.append(payload)
    return result


def kernel_request(image, arguments, memory, threads, block, output, cooperative=False, context=None):
    """The emulator's whole-kernel request for `image` (`sass.decode.cubin.kernel_image`) launched as
    `threads` threads in blocks of `block`: `arguments` are the kernel parameter values in order, packed
    into constant bank 0 with the launch dimensions, local stack base and generic windows (also in every
    thread's SR_*WIN* registers); `memory` adds regions
    [{space, address, hex}]; `output` is {address, size, stride}, read back per thread."""
    if image["arch"] not in LAUNCH_LAYOUT: raise ValueError(f"{image['arch']}: launch constant layout unknown (known: {', '.join(LAUNCH_LAYOUT)})")
    payloads = serialize_arguments((p["size"] for p in image["parameters"]), arguments)
    dims, stack, shared, local = LAUNCH_LAYOUT[image["arch"]]
    constants = dict(image["constants"]); bank = bytearray(constants.get(0, b""))
    bank.extend(bytes(max(0, image["parameter_base"] + image["parameter_size"] - len(bank))))
    struct.pack_into("<III", bank, dims, block, 1, 1)
    struct.pack_into("<I", bank, stack, 0x1000)
    for offset, window in ((shared, "SWIN"), (local, "LWIN")):
        if offset is not None: struct.pack_into("<Q", bank, offset, WINDOWS[window][0])
    for parameter, payload in zip(image["parameters"], payloads):
        offset = image["parameter_base"] + parameter["offset"]
        bank[offset:offset + parameter["size"]] = payload
    constants[0] = bytes(bank)
    return dict(kernel=dict(name=image["name"], code=image["code"].hex(), threads=threads, block_size=block, cooperative=cooperative,
                            registers=window_registers()),
                memory=[dict(space="cbank", address=index << 32, hex=value.hex()) for index, value in constants.items()] + memory,
                output=output, context=context or {})
