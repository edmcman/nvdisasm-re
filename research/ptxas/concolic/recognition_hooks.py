"""Extract IJON sites for registered opcode and reserved-token recognition.

No PTX spelling inventory: sites come from the pinned lexer's action table.
Usage: python3 recognition_hooks.py /path/to/ptxas OUTPUT.ijon
"""
import struct, sys
from pathlib import Path
from pinned import loader

def hooks(binary):
    read=loader(binary)
    targets=struct.unpack('<552Q',read(0x203a5a8,552*8))
    # Audited rule partition: 60..526 are reserved keywords, modifier enums,
    # built-in registers and type rules. General identifiers are 528/529;
    # numeric/string patterns and scanner machinery live outside this partition.
    sites=sorted(set(targets[60:527]))
    assert len(sites)==467 and all(0x721000<=a<0x724c80 for a in sites)
    lines=['# Successful registered-opcode lookup: EAX is its stable opcode ID.',
           '0x46c6b4, ijon_set, eax, 4',
           '# Dedicated reserved-token actions only; RAX still holds the action ID.']
    lines.extend(f'{a:#x}, ijon_set, eax, 4' for a in sites)
    return '\n'.join(lines)+'\n'

if __name__=='__main__': Path(sys.argv[2]).write_text(hooks(sys.argv[1]))
