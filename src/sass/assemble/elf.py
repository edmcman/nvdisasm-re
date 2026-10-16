"""Native ELF64 CUDA executable writer for self-contained SASS kernels.

ABI v8 profiles are established from CUDA 13.0 ptxas probes, not guessed from the
instruction architecture. SM107 remains raw-only until its executable ABI is established.
"""
import struct
from dataclasses import dataclass

from sass.assemble import encoding, instruction, source
from sass.assemble.instruction import AssemblyError


@dataclass(frozen=True)
class Profile:
    sm: int
    parameter_base: int
    mercury: int = 0
    software_workarounds: int = 0


PROFILES = {
    'SM75': Profile(75, 0x160, software_workarounds=1),
    'SM80': Profile(80, 0x160),
    'SM86': Profile(86, 0x160),
    'SM89': Profile(89, 0x160),
    'SM90': Profile(90, 0x210, 0x101, 8),
    'SM100': Profile(100, 0x380, 0x101, 8),
    # CUDA 13 renamed SM101 to SM110; the repository retains the md family name.
    'SM101': Profile(110, 0x380, 0x101, 8),
    'SM103': Profile(103, 0x380, 0x101, 8),
    'SM120': Profile(120, 0x380, 0x101),
}


def record(attribute, payload):
    return struct.pack('<BBH', 4, attribute, len(payload)) + payload


def inline(attribute, value=0, fmt=3):
    return struct.pack('<BBH', fmt, attribute, value)


@dataclass
class Section:
    name: str
    type: int = 1
    flags: int = 0
    alignment: int = 1
    body: bytes = b''
    link: int = 0
    info: int = 0
    entsize: int = 0
    size: int | None = None
    offset: int = 0


def metadata(kernel, profile, constant_symbol):
    out = record(0x37, struct.pack('<I', 130))  # EIATTR_CUDA_API_VERSION
    out += inline(0x1b, 255)                  # EIATTR_MAXREG_COUNT
    out += inline(0x5f, profile.mercury)      # EIATTR_MERCURY_ISA_VERSION
    if profile.sm >= 90:
        out += inline(0x50)                  # EIATTR_SPARSE_MMA_MASK
    if profile.sm >= 100:
        out += inline(0x4a, fmt=2)           # EIATTR_VRC_CTA_INIT_COUNT
    if profile.software_workarounds or profile.sm >= 90:
        out += record(0x36, struct.pack('<I', profile.software_workarounds))
    # Include even the empty layout, making zero-parameter kernels inspectable by kernel_image.
    out += record(0x0a, struct.pack('<IHH', constant_symbol, profile.parameter_base, kernel.parameter_size))
    out += inline(0x19, kernel.parameter_size)
    for ordinal, param in reversed(list(enumerate(kernel.parameters))):
        # EIATTR_KPARAM_INFO: byte size, space=generic, cbank parameter, log alignment=0.
        flags = param.size << 18 | 0x1f000
        out += record(0x17, struct.pack('<IHHI', 0, ordinal, param.offset, flags))
    barriers = [env.get('Sb', 15) + 1 for k, env, _, _ in kernel.instructions if k.mnemonic == 'BAR']
    if barriers:
        out += inline(0x4c, max(barriers), fmt=2)  # EIATTR_NUM_BARRIERS; register IDs reserve all 16
    offsets = [i * 16 for i, (k, _, _, _) in enumerate(kernel.instructions) if k.mnemonic == 'EXIT']
    if offsets:
        out += record(0x1c, struct.pack('<' + 'I' * len(offsets), *offsets))
    # These offsets let the driver patch cooperative/block barrier operations when needed.
    ctaid, ctaidz = [], False
    for i, (k, env, _, _) in enumerate(kernel.instructions):
        if k.mnemonic in ('S2R', 'S2UR'):
            from sass.decode import ir
            md = ir.load('SM' + str(profile.sm) if profile.sm != 110 else 'SM101')
            registers = [a for a in k.operand_types.values() if a.type == 'SpecialRegister']
            if any(env.get(a.name) == md.enums[a.type].get('SR_CTAID.Z') for a in registers):
                ctaidz = True
            if k.mnemonic == 'S2R' and any(env.get(a.name) in
                   {v for n, v in md.enums[a.type].items() if n.startswith('SR_CTAID')} for a in registers):
                ctaid.append(i * 16)
    if ctaid:
        out += record(0x1d, struct.pack('<' + 'I' * len(ctaid), *ctaid))
    if ctaidz:
        out += inline(4, fmt=1)              # EIATTR_CTAIDZ_USED
    return out


def build(module, filename='<string>'):
    if module.arch not in PROFILES:
        raise AssemblyError(f'{module.arch}: cubin ABI is not established; use --format raw', filename)
    profile = PROFILES[module.arch]
    for kernel in module.kernels:
        kernel.code, kernel.instructions = source.sequence(
            module.arch, kernel.statements, symbols=source.symbol_values(kernel, profile.parameter_base), filename=filename)
        if not kernel.code:
            raise AssemblyError(f'{kernel.name}: kernel has no instructions', filename)
        for k, _, _, statement in kernel.instructions:
            props = encoding.classes(module.arch)[k.name]['props']
            if props.get('BRANCH_TYPE') in ('BRT_CALL', 'BRT_RETURN') or k.mnemonic in ('CALL', 'JCAL', 'RET'):
                raise source.error(ValueError('device calls and returns require an unsupported device-function ABI; use raw output'), statement, filename)
        source.validate_resources(module.arch, kernel, filename)
        # Instruction fetch reads complete aligned blocks; padding never alters label addresses.
        nop = instruction.assemble(module.arch, 'NOP')[0]
        kernel.code += nop * ((source.align(len(kernel.code), 256) - len(kernel.code)) // 16)

    return link(module)


def link(module):
    """Link prepared Kernel.code/instructions into a native cubin.

    Used by the test harness to retain exact encoded probe words. Callers must
    supply resource declarations, instruction metadata and fetch-block padding.
    Normal source assembly goes through build(), including its validation.
    """
    profile = PROFILES[module.arch]
    sections = [Section('', type=0), Section('.shstrtab', type=3), Section('.strtab', type=3),
                Section('.symtab', type=2, alignment=8, link=2, entsize=24),
                Section('.nv.info', type=0x70000000, alignment=4, link=3)]
    # CUDA's loader uses this note to identify the toolkit ABI, independently of e_flags.
    strings = bytearray(b'\0')
    offsets = []
    for value in ('sass-assemble', 'nvdisasm-re', 'native', ''):
        offsets.append(len(strings)); strings.extend(value.encode() + b'\0')
    descriptor = struct.pack('<6I', 2, 0, *offsets) + strings
    descriptor += bytes(-len(descriptor) % 4)
    tkinfo = struct.pack('<III', 12, len(descriptor), 2000) + b'NVIDIA Corp\0' + descriptor
    sections.append(Section('.note.nv.tkinfo', type=7, flags=0x2000000, alignment=4, body=tkinfo))
    cuinfo = struct.pack('<III', 12, 8, 1000) + b'NVIDIA Corp\0' + struct.pack('<HHI', 2, profile.sm, 130)
    sections.append(Section('.note.nv.cuinfo', type=7, flags=0x1000040, alignment=4, link=5, body=cuinfo))
    if profile.mercury:
        # CUDA 13 native executables describe their ISA compatibility independently of
        # optional .nv.merc.* intermediate-code sections. No opportunistic finalization
        # or fastpath claims are made for hand-assembled code.
        compat = (inline(9, fmt=2) + inline(2, 1, fmt=2) + inline(5, 5, fmt=2)
                  + inline(7, profile.mercury) + inline(3, fmt=2) + inline(6, fmt=2)
                  + record(11, bytes(8)))
        sections.append(Section('.nv.compat', type=0x70000086, alignment=4, body=compat))
    sections.append(Section('.nv.callgraph', type=0x70000001, alignment=4, link=3, entsize=8,
                            body=b''.join(struct.pack('<Ii', 0, sentinel) for sentinel in (-1, -2, -3, -4))))
    groups = []
    global_constants = {}
    infos, constants = {}, {}
    for kernel in module.kernels:
        infos[kernel.name] = len(sections)
        sections.append(Section('.nv.info.' + kernel.name, type=0x70000000, flags=0x40, alignment=4, link=3))
    for kernel in module.kernels:
        constants[kernel.name] = len(sections)
        sections.append(Section('.nv.constant0.' + kernel.name, flags=0x42, alignment=4,
                                body=bytes(profile.parameter_base + kernel.parameter_size)))
    for kernel in module.kernels:
        text_index = len(sections)
        sections.append(Section('.text.' + kernel.name, flags=6, alignment=128, body=kernel.code, link=3))
        const_index, info_index = constants[kernel.name], infos[kernel.name]
        sections[const_index].info = sections[info_index].info = text_index
        if kernel.shared:
            sections.append(Section('.nv.shared.' + kernel.name, type=8, flags=3, alignment=16,
                                    info=text_index, size=kernel.shared))
        for bank, body in sorted(kernel.constants.items()):
            if bank == 2:
                sections.append(Section(f'.nv.constant{bank}.{kernel.name}', flags=0x42, alignment=4,
                                        body=body, info=text_index))
            else:
                if bank in global_constants and global_constants[bank] != body:
                    raise AssemblyError(f'constant bank {bank} is module-wide; kernels declare conflicting contents')
                global_constants[bank] = body
        groups.append((kernel, text_index, const_index, info_index))

    for bank, body in sorted(global_constants.items()):
        sections.append(Section(f'.nv.constant{bank}', flags=2, alignment=4, body=body))

    strings = bytearray(b'\0')
    names = {}
    def string(name):
        if name not in names:
            names[name] = len(strings)
            strings.extend(name.encode() + b'\0')
        return names[name]
    symbols = [bytes(24)]
    section_symbols = {}
    for i, section in enumerate(sections):
        if i >= 4:
            section_symbols[i] = len(symbols)
            symbols.append(struct.pack('<IBBHQQ', string(section.name), 3, 0, i, 0, 0))
    sections[3].info = len(symbols)  # first non-local symbol
    global_info = b''
    for kernel, text, const, info in groups:
        sym = len(symbols)
        symbols.append(struct.pack('<IBBHQQ', string(kernel.name), 0x12, 0x10, text, 0, len(kernel.code)))
        sections[text].info = kernel.registers << 24 | sym
        sections[info].body = metadata(kernel, profile, section_symbols[const])
        for attribute, value in ((0x2f, kernel.registers), (0x11, kernel.frame_size), (0x12, kernel.frame_size)):
            global_info += record(attribute, struct.pack('<II', sym, value))
    sections[4].body = global_info
    sections[3].body = b''.join(symbols)
    sections[2].body = bytes(strings)
    shstrings = bytearray(b'\0')
    shnames = [0]
    for section in sections[1:]:
        shnames.append(len(shstrings)); shstrings.extend(section.name.encode() + b'\0')
    sections[1].body = bytes(shstrings)

    image = bytearray(64)
    # Keep allocated sections contiguous, followed by non-allocated tables/metadata.
    order = list(range(1, len(sections)))
    alloc_start, alloc_end, memory_size = None, 0, 0
    for i in order:
        section = sections[i]
        image.extend(bytes(source.align(len(image), section.alignment) - len(image)))
        section.offset = len(image)
        if section.flags & 2:
            if alloc_start is None:
                alloc_start = section.offset
            if section.type == 8:
                memory_size += section.size
        if section.type != 8:
            image.extend(section.body)
        if section.flags & 2:
            alloc_end = len(image)
    image.extend(bytes(source.align(len(image), 8) - len(image)))
    shoff = len(image)
    for i, section in enumerate(sections):
        size = section.size if section.size is not None else len(section.body)
        image.extend(struct.pack('<IIQQQQIIQQ', shnames[i], section.type, section.flags, 0,
                                 section.offset, size, section.link, section.info, section.alignment if i else 0, section.entsize))
    phoff = len(image)
    # PT_PHDR plus the allocated image, matching CUDA's address-zero executable convention.
    phsize = 3 * 56
    image.extend(struct.pack('<IIQQQQQQ', 6, 5, phoff, 0, 0, phsize, phsize, 8))
    image.extend(struct.pack('<IIQQQQQQ', 1, 5, alloc_start, 0, 0, alloc_end - alloc_start,
                             alloc_end - alloc_start + memory_size, 8))
    image.extend(struct.pack('<IIQQQQQQ', 1, 5, phoff, 0, 0, phsize, phsize, 8))
    ident = b'\x7fELF\x02\x01\x01\x41\x08' + bytes(7)
    flags = 0x6000000 | profile.sm << 8 | (2 if profile.sm >= 100 else 4)
    image[:64] = struct.pack('<16sHHIQQQIHHHHHH', ident, 2, 190, 1, 0, phoff, shoff, flags,
                             64, 56, 3, 64, len(sections), 1)
    return bytes(image)
