"""Kernel launch state shared by the emulator's whole-kernel requests and the GPU harness: the
launch constants' bank-0 layout and the generic memory windows."""
import struct
from math import prod


# Launch constants in bank 0: (blockDim.xyz, local stack base, shared window, local window) offsets, from
# nvcc 13.0 probe kernels reading each value (2026-10-15; docs/testing.md). SM90+ build the generic shared
# base from SR_SWINHI alone (None). SM107 has no nvcc target to check.
LAUNCH_LAYOUT = {**dict.fromkeys(("SM75", "SM80", "SM86", "SM89"), (0x0, 0x28, 0x18, 0x20)), "SM90": (0x0, 0x28, None, 0x20),
                 **dict.fromkeys(("SM100", "SM101", "SM103", "SM120"), (0x360, 0x37c, None, 0x2f8))}
# The emulated generic windows (base, size): 4 GiB-aligned like SM90+'s shared base, 16 MiB like SM89's.
WINDOWS = dict(SWIN=(0x7f00_00000000, 1 << 24), LWIN=(0x7e00_00000000, 1 << 24))


GLOBAL_DESCRIPTOR = {'SM89': 0x118, 'SM90': 0x208}
GRID_LAYOUT = {arch: dims + (12 if dims == 0 else 16) for arch, (dims, *_) in LAUNCH_LAYOUT.items()}


def dimensions(value):
    match value:
        case int() if not isinstance(value, bool): axes = (value,)
        case tuple() | list(): axes = tuple(value)
        case _: raise ValueError("dimensions must be an integer or one-to-three integers")
    if not 1 <= len(axes) <= 3 or any(type(n) is not int or not 0 < n < 1 << 32 for n in axes):
        raise ValueError("dimensions must contain one-to-three positive uint32 integers")
    return axes + (1,) * (3 - len(axes))


def geometry(threads=None, block=32, grid=None):
    block = dimensions(block); size = prod(block)
    if threads is not None and (type(threads) is not int or not 0 < threads < 1 << 64):
        raise ValueError("threads must be a positive uint64 integer")
    if grid is None:
        if threads is None: raise ValueError("threads or grid is required")
        grid = dimensions(-(-threads // size))
    else:
        grid = dimensions(grid)
        count = prod(grid) * size
        if count >= 1 << 64: raise ValueError("launch thread count exceeds uint64")
        if threads is not None and threads != count: raise ValueError("threads does not match grid and block")
        threads = count
    if size >= 1 << 64: raise ValueError("block thread count exceeds uint64")
    return grid, block, threads


def coordinates(rank, dims):
    x, y, _ = dims
    return rank % x, rank // x % y, rank // (x * y)


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


def kernel_request(image, arguments, memory, threads=None, block=32, output=None, cooperative=False, context=None, *, grid=None):
    """The emulator's whole-kernel request for `image` (`sass.decode.cubin.kernel_image`) launched as
    `threads` threads in blocks of `block`: `arguments` are the kernel parameter values in order, packed
    into constant bank 0 with the launch dimensions, local stack base and generic windows (also in every
    thread's SR_*WIN* registers); `memory` adds regions
    [{space, address, hex}]; `output` is {address, size, stride}, read back per thread."""
    if image["arch"] not in LAUNCH_LAYOUT: raise ValueError(f"{image['arch']}: launch constant layout unknown (known: {', '.join(LAUNCH_LAYOUT)})")
    explicit = grid is not None
    grid, block, threads = geometry(threads, block, grid)
    payloads = serialize_arguments((p["size"] for p in image["parameters"]), arguments)
    dims, stack, shared, local = LAUNCH_LAYOUT[image["arch"]]
    constants = dict(image["constants"]); bank = bytearray(constants.get(0, b""))
    bank.extend(bytes(max(0, max(image["parameter_base"] + image["parameter_size"], stack + 4,
                                GRID_LAYOUT[image["arch"]] + 12, (local or 0) + 8, (shared or 0) + 8) - len(bank))))
    struct.pack_into("<III", bank, dims, *block)
    struct.pack_into("<III", bank, GRID_LAYOUT[image["arch"]], *grid)
    struct.pack_into("<I", bank, stack, 0x1000)
    for offset, window in ((shared, "SWIN"), (local, "LWIN")):
        if offset is not None: struct.pack_into("<Q", bank, offset, WINDOWS[window][0])
    for parameter, payload in zip(image["parameters"], payloads):
        offset = image["parameter_base"] + parameter["offset"]
        bank[offset:offset + parameter["size"]] = payload
    constants[0] = bytes(bank)
    return dict(kernel=dict(name=image["name"], code=image["code"].hex(), threads=threads, block_size=prod(block), cooperative=cooperative,
                            registers=window_registers(), **(dict(grid=grid, block=block) if explicit else
                                                           dict(block=block) if block[1:] != (1, 1) else {})),
                memory=[dict(space="cbank", address=index << 32, hex=value.hex()) for index, value in constants.items()] + memory,
                output=output, context=context or {})
