"""Architecture-specific labels for the driver's constant-bank-0 ABI."""
from xml.etree import ElementTree
from sass import launch
from sass.assemble import elf


# Compiler-established ABI slots (docs/testing.md, launch constants), including targets
# without a GPU oracle. launch.GLOBAL_DESCRIPTOR is the GPU harness's supported subset.
GLOBAL_DESCRIPTOR = {
    **launch.GLOBAL_DESCRIPTOR,
    **dict.fromkeys(("SM80", "SM86"), 0x118),
    **dict.fromkeys(("SM100", "SM101", "SM103", "SM120"), 0x358),
}
KERNEL_ARGUMENT_BYTES = 4096


def render(template, arch):
    """Extend the shared processor spec with established launch fields and argument base."""
    root = ElementTree.fromstring(template)
    symbols = []
    if arch in launch.LAUNCH_LAYOUT:
        dims, stack, shared, local = launch.LAUNCH_LAYOUT[arch]
        symbols.extend((f"blockDim_{axis}", dims + i * 4, 4, f"Block dimension {axis}")
                       for i, axis in enumerate("xyz"))
        symbols.append(("local_stack_base", stack, 4, "Per-thread local stack base"))
        for name, offset in (("shared_window_base", shared), ("local_window_base", local)):
            if offset is not None:
                symbols.append((name, offset, 8, "Generic address window base"))
    if arch in GLOBAL_DESCRIPTOR:
        symbols.append(("default_global_descriptor", GLOBAL_DESCRIPTOR[arch], 8,
                        "Driver-provided default global-memory descriptor"))
    if arch in elf.PROFILES:
        symbols.append(("kernel_arguments", elf.PROFILES[arch].parameter_base, KERNEL_ARGUMENT_BYTES,
                        "Kernel argument byte region; individual sizes and offsets are kernel-specific"))
    if symbols:
        defaults = ElementTree.SubElement(root, "default_symbols")
        for name, address, size, description in sorted(symbols, key=lambda s: s[1]):
            ElementTree.SubElement(defaults, "symbol", name=name, address=f"cbank:{address:#x}",
                                   size=str(size), entry="false", description=description)
        blocks = ElementTree.SubElement(root, "default_memory_blocks")
        ElementTree.SubElement(blocks, "memory_block", name="constant_bank_0", start_address="cbank:0x0",
                               length="0x10000", initialized="false", mode="r")
    ElementTree.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"
