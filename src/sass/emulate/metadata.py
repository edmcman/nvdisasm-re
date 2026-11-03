"""Decoded instruction facts consumed by the common scheduler."""
import json
from sass.decode import ir, pydecode
from sass.emulate import schedule

FILE_IDS = {"R": 0, "UR": 1, "SR": 2, "P": 3, "UP": 4}


def operand(location):
    """A C++ sass::Operand from schedule.operand_location (None: no operand, reading 0)."""
    match location:
        case None: return "{0, 0, 0, 0, 0}"
        case "const", value: return f"{{1, 0, 0, 0, UINT64_C({value:#x})}}"
        case file, index, span: return f"{{2, {FILE_IDS[file]}, {index}, {span}, 0}}"


class Instruction:
    def __init__(self, arch, word):
        self.decoded = self.error = None
        try: self.decoded = pydecode.decode(arch, word)
        except pydecode.NoMatch as error: self.error = str(error)

    def unsupported(self, arch):
        d = self.decoded
        if d is None: return None
        k = d.klass
        labels = {n: ir.load(arch).rev_enums[a.type].get(d.env[n], '')
                  for n, a in k.operand_types.items() if n in ('sem', 'sco') and n in d.env}
        if k.mnemonic == 'BAR' and (k.name != 'bar__SYNC_II_optionalCount_II' or d.env.get('Sb', 0) or d.env.get('Sc', 0)):
            return 'unsupported barrier in launch scope: ' + d.text
        if k.mnemonic in ('BSSY', 'BSYNC', 'BREAK', 'BMOV'):
            return 'unsupported synchronization in launch scope: ' + d.text
        if k.mnemonic in ('ATOM', 'ATOMG', 'ATOMS', 'RED', 'REDG', 'MEMBAR', 'FENCE') or labels.get('sem') == 'MMIO' or labels.get('sco') in ('SYS', 'VC'):
            return 'unsupported ordering in launch scope: ' + d.text
        return None

    def info(self, arch, i, dests=()):
        d = self.decoded
        if d is None: return f"{{0, 0, 0, 0, 0, 0, 0, 0, {operand(None)}, {operand(None)}, {operand(None)}, {operand(None)}, 0, {json.dumps(self.error)}, nullptr, nullptr}}"
        k, env = d.klass, d.env
        location = lambda name: schedule.operand_location(arch, d, name) if name in k.operand_types else None
        key, source = schedule.WARP_INPUTS.get(k.mnemonic, (None, None))
        sync, mask, sync_operand = 0, 0, None
        if k.mnemonic == "WARPSYNC":
            match schedule.sync_form(arch, d):
                case "all",: sync = 1
                case "immediate", mask: sync = 2
                case "operand", name: sync, sync_operand = 3, location(name)
        fields = [int(k.mnemonic == "EXIT"), int(k.name.startswith("bar__SYNC")), int(k.mnemonic == "WARPSYNC"),
                  {None: 0, "values": 1, "predicates": 2}[key], env.get(("Pg", "not"), 0), env.get(("Pp", "not"), 0), sync, len(dests)]
        operands = [location("Pg"), location("Pp") if "Pp" in k.operand_types else ("const", 1), location(source) if source else None, sync_operand]
        return "{" + ", ".join([*map(str, fields), *map(operand, operands), f"UINT64_C({mask:#x})", "nullptr", f"D{i}" if dests else "nullptr", json.dumps(self.unsupported(arch)) if self.unsupported(arch) else "nullptr"]) + "}"
