"""Decoded instruction facts consumed by the common scheduler."""
import json
from sass.decode import ir, pydecode
from sass.emulate import schedule
from sass.md import parse
from sass.semantics import primitive

FILE_IDS = {"R": 0, "UR": 1, "SR": 2, "P": 3, "UP": 4}


def operand(location):
    """A C++ sass::Operand from schedule.operand_location (None: no operand, reading 0)."""
    match location:
        case None: return "{0, 0, 0, 0, 0}"
        case "const", value: return f"{{1, 0, 0, 0, UINT64_C({value:#x})}}"
        case file, index, span: return f"{{2, {FILE_IDS[file]}, {index}, {span}, 0}}"


class Instruction:
    def __init__(self, arch, word):
        self.word = int.from_bytes(word, 'little')
        self.decoded = self.error = None
        try: self.decoded = pydecode.decode(arch, word)
        except pydecode.NoMatch as error: self.error = str(error)

    def unsupported_scope(self, arch):
        d = self.decoded
        if d is None: return 0
        k = d.klass
        labels = {n: ir.load(arch).rev_enums[a.type].get(d.env[n], '')
                  for n, a in k.operand_types.items() if n in ('sem', 'sco') and n in d.env}
        return 2 if k.mnemonic in ('ATOM', 'ATOMG', 'ATOMS', 'RED', 'REDG', 'MEMBAR', 'FENCE') or labels.get('sem') == 'MMIO' or labels.get('sco') in ('SYS', 'VC') else 1

    def unsupported(self, arch):
        d = self.decoded
        if d is None: return None
        k = d.klass
        if k.mnemonic == 'BAR' and not k.name.startswith(('bar__SYNC_', 'bar__SYNC_dfrBlk_', 'bar__RED_', 'bar__ARV_')):
            return 'unsupported barrier in cooperative scope: ' + d.text
        if k.mnemonic == 'BAR' and {'Rb', 'Rc'} <= k.operand_types.keys():
            return 'unresolved packed-register barrier: ' + d.text
        if k.mnemonic in ('BREAK', 'BMOV'):
            return 'unsupported synchronization in cooperative scope: ' + d.text
        if self.unsupported_scope(arch) == 2 and k.mnemonic not in ('ATOMG', 'MEMBAR'):
            return 'unsupported ordering in launch scope: ' + d.text
        return None

    def info(self, arch, i, dests=()):
        d = self.decoded
        if d is None: return f"{{0, 0, 0, 0, 0, 0, 0, 0, {operand(None)}, {operand(None)}, {operand(None)}, {operand(None)}, 0, {json.dumps(self.error)}, nullptr, nullptr}}"
        k, env = d.klass, d.env
        def location(name):
            if name not in k.operand_types: return None
            match k.operand_types[name].type:
                case 'UImm' | 'SImm': return 'const', env[name]
                case _: return schedule.operand_location(arch, d, name)
        key, source = schedule.WARP_INPUTS.get(k.mnemonic, (None, None))
        sync, mask, sync_operand = 0, 0, None
        if k.mnemonic == "WARPSYNC":
            match schedule.sync_form(arch, d):
                case "all",: sync = 1
                case "immediate", mask: sync = 2
                case "operand", name: sync, sync_operand = 3, location(name)
        fields = [int(k.mnemonic == "EXIT"), int(k.name.startswith(("bar__SYNC", "bar__RED", "bar__ARV"))), int(k.mnemonic == "WARPSYNC"),
                  {None: 0, "values": 1, "predicates": 2}[key], env.get(("Pg", "not"), 0), env.get(("Pp", "not"), 0), sync, len(dests)]
        operands = [location("Pg"), location("Pp") if "Pp" in k.operand_types else ("const", 1), location(source) if source else None, sync_operand]
        convergence = {'BSSY': 1, 'BSYNC': 2}.get(k.mnemonic, 0)
        target = env.get('Sa', 0)
        extra = [str(convergence), str(env.get('barReg', 0)), str(target),
                 operand(location('Sb' if 'Sb' in k.operand_types else 'Rb') if k.mnemonic == 'BAR' else None),
                 operand(location('Sc' if 'Sc' in k.operand_types else 'Rc') if k.mnemonic == 'BAR' else None),
                 str(int('dfrBlk' in k.name)),
                 str(int('DECOUPLED' in parse.arch_classes(arch)[k.name]['props'].get('INSTRUCTION_TYPE', ''))),
                 str(self.word >> 116 & 63), str(self.word >> 113 & 7), str(self.word >> 110 & 7),
                 str(self.unsupported_scope(arch)),
                 "{" + ", ".join(operand(location(name)) if name in schedule.WARP_GROUPS.get(k.mnemonic, ())
                                and name in k.operand_types else operand(None) for name in primitive.OPERAND_GROUPS) + "}",
                 str({"AND": 1, "OR": 2, "POPC": 3}.get(ir.load(arch).rev_enums[k.operand_types["op"].type][env["op"]], 0)
                     if k.name.startswith("bar__RED") else 0),
                 str(int(k.name.startswith("bar__ARV"))),
                 str(int(k.mnemonic == "B2R" and "barname" in k.operand_types))]
        if k.mnemonic in schedule.WARP_GROUPS: fields[3] = 3
        return "{" + ", ".join([*map(str, fields), *map(operand, operands), f"UINT64_C({mask:#x})", "nullptr", f"D{i}" if dests else "nullptr", json.dumps(self.unsupported(arch)) if self.unsupported(arch) else "nullptr", *extra]) + "}"
