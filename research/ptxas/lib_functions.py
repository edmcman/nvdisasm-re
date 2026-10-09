"""Per-function facts from libnvptxcompiler_static.a members (CUDA 13.0.88).

Each archive member keeps its source file (STT_FILE) and puts each function in its
own section, so relocations give exact string references and call sequences.

  python3 lib_functions.py EXTRACTED_ARCHIVE_DIR OUT.json
Output: {member:symbol: {source, size, strings: [...], calls: [symbol in call order]}}.
"""
import json, sys
from pathlib import Path
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

CALL_RELOCS = {2, 4}  # R_X86_64_PC32, R_X86_64_PLT32


def cstring(data, off):
    end = data.find(b'\0', off)
    raw = data[off:end] if end >= 0 else b''
    return raw.decode() if raw and all(32 <= c < 127 or c in (9, 10) for c in raw) else None


def member_functions(path):
    elf = ELFFile(open(path, 'rb'))
    symtab = elf.get_section_by_name('.symtab')
    symbols = list(symtab.iter_symbols())
    source = next((s.name for s in symbols if s['st_info']['type'] == 'STT_FILE'), '?')
    by_section = {}
    for s in symbols:
        if s['st_info']['type'] == 'STT_FUNC' and isinstance(s['st_shndx'], int):
            by_section.setdefault(s['st_shndx'], []).append(s)
    for fns in by_section.values(): fns.sort(key=lambda s: s['st_value'])
    out = {s.name: dict(source=source, size=s['st_size'], strings=[], calls=[])
           for fns in by_section.values() for s in fns}

    def owner(shndx, off):
        """Function in section shndx containing offset off."""
        return next((s for s in reversed(by_section.get(shndx, [])) if s['st_value'] <= off), None)

    for sec in elf.iter_sections():
        if not isinstance(sec, RelocationSection) or sec['sh_info'] not in by_section: continue
        text = elf.get_section(sec['sh_info']).data()
        for r in sorted(sec.iter_relocations(), key=lambda r: r['r_offset']):
            fn = owner(sec['sh_info'], r['r_offset'])
            if fn is None: continue
            sym = symbols[r['r_info_sym']]
            pc_rel = 4 if r['r_info_type'] in CALL_RELOCS else 0
            if r['r_info_type'] in CALL_RELOCS and text[r['r_offset'] - 1] == 0xE8:
                if sym['st_info']['type'] == 'STT_SECTION':
                    callee = owner(sym['st_shndx'], r['r_addend'] + pc_rel)
                    if callee is not None: out[fn.name]['calls'].append(callee.name)
                else:
                    out[fn.name]['calls'].append(sym.name)
                continue
            if not isinstance(sym['st_shndx'], int): continue
            target = elf.get_section(sym['st_shndx'])
            if target.name.startswith('.rodata'):
                s = cstring(target.data(), sym['st_value'] + r['r_addend'] + pc_rel)
                if s: out[fn.name]['strings'].append(s)
    return out


def main(objdir, out):
    table = {}
    for obj in sorted(Path(objdir).glob('*.o')):
        for sym, info in member_functions(obj).items():
            table[f'{obj.name}:{sym}'] = info
    Path(out).write_text(json.dumps(table))
    print(len(table), 'functions;', sum(bool(v['strings']) for v in table.values()), 'reference strings')


if __name__ == '__main__':
    main(*sys.argv[1:])
