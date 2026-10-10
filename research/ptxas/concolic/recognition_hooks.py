"""Extract IJON sites for registered opcode and reserved-token recognition.

No PTX spelling inventory: sites come from the pinned lexer's action table.
Usage: python3 recognition_hooks.py /path/to/ptxas OUTPUT.ijon
"""
import hashlib, struct, sys
from pathlib import Path
PINNED='daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'

def hooks(binary):
    data=Path(binary).read_bytes()
    if hashlib.sha256(data).hexdigest()!=PINNED: raise ValueError('Requires pinned CUDA 13.0.88 ptxas')
    phoff=struct.unpack_from('<Q',data,32)[0]
    ents,count=struct.unpack_from('<HH',data,54)
    def read(address,size):
        for i in range(count):
            kind,_,offset,vaddr,_,length,_,_=struct.unpack_from('<IIQQQQQQ',data,phoff+i*ents)
            if kind==1 and vaddr<=address and address+size<=vaddr+length:
                return data[offset+address-vaddr:offset+address-vaddr+size]
        raise ValueError('Unmapped address')
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
