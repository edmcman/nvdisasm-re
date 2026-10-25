"""SASS source assembly and native CUDA ELF output."""
from sass.assemble.instruction import AssemblyError


def assemble_instruction(arch, text, addr=0):
    from sass.assemble import instruction, source
    statements = source.statements(text if text.rstrip().endswith(';') else text + ';')
    if len(statements) != 1:
        raise AssemblyError('expected exactly one instruction')
    try:
        return instruction.assemble(source.target(arch), statements[0].text, addr)[0]
    except ValueError as exc:
        raise AssemblyError(str(exc)) from None


def assemble_raw(arch, source, base=0):
    from sass.assemble.source import raw
    return raw(arch, source, base)


def assemble_cubin(source):
    from sass.assemble import elf
    from sass.assemble.source import module
    return elf.build(module(source))
