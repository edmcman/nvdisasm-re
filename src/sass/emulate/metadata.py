"""Decoded instruction facts consumed by the common scheduler."""
import json
from sass.decode import ir, pydecode
from sass.emulate import schedule
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
        if k.mnemonic == 'WARPSYNC' and not primitive.supported_warp_sync(k):
            return 'unsupported warp synchronization in cooperative scope: ' + d.text
        if self.unsupported_scope(arch) == 2 and k.mnemonic not in ('ATOMG', 'MEMBAR'):
            return 'unsupported ordering in launch scope: ' + d.text
        return None

    def info(self, arch, i, dests=()):
        d = self.decoded
        if d is None: return f"{{0, 0, 0, 0, {operand(None)}, {json.dumps(self.error)}, nullptr, nullptr}}"
        k, env = d.klass, d.env
        def location(name):
            if name not in k.operand_types: return None
            match k.operand_types[name].type:
                case 'UImm' | 'SImm': return 'const', env[name]
                case _: return schedule.operand_location(arch, d, name)
        fields = [int(k.mnemonic == "EXIT"), int(k.mnemonic in schedule.WARP_CONTEXT),
                  env.get(("Pg", "not"), 0), len(dests)]
        operands = [location("Pg")]
        extra = [str(self.unsupported_scope(arch))]
        return "{" + ", ".join([*map(str, fields), *map(operand, operands), "nullptr", f"D{i}" if dests else "nullptr", json.dumps(self.unsupported(arch)) if self.unsupported(arch) else "nullptr", *extra]) + "}"
