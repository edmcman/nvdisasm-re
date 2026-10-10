"""Static reads from the pinned CUDA 13.0.88 ptxas by virtual address."""
import hashlib, struct
from pathlib import Path
PINNED='daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'

def loader(binary):
    data=Path(binary).read_bytes()
    if hashlib.sha256(data).hexdigest()!=PINNED: raise ValueError('Requires pinned CUDA 13.0.88 ptxas')
    phoff=struct.unpack_from('<Q',data,32)[0]
    ents,count=struct.unpack_from('<HH',data,54)
    segments=[struct.unpack_from('<IIQQQQQQ',data,phoff+i*ents) for i in range(count)]
    def read(address,size):
        for kind,_,offset,vaddr,_,length,_,_ in segments:
            if kind==1 and vaddr<=address and address+size<=vaddr+length:
                return data[offset+address-vaddr:offset+address-vaddr+size]
        raise ValueError('Unmapped address')
    return read
