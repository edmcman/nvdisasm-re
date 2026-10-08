"""Deterministic semantic fixtures assembled from the extracted md rules."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sass import ir,pydecode
from sass.gen_sleigh import default_value,ENUM_FILE,REG_FILES


def encode(archname,classname,**values):
    arch=ir.load(archname);k=next(c for c in arch.classes if c.name==classname)
    desired={}
    for atom in k.operand_types.values():
        value=values.get(atom.name,default_value(arch,atom))
        if value is None and atom.type in ENUM_FILE:
            value=REG_FILES[ENUM_FILE[atom.type]][3] if atom.kind=='guard' or 'Predicate' in atom.type else 0
        if value is None:value=min(arch.enums[atom.type].values()) if atom.type in arch.enums else 0
        if isinstance(value,str):value=arch.enums[atom.type][value]
        desired[atom.name]=value
    desired.update({tuple(n.split('@')):v for n,v in values.items() if '@' in n})
    word=k.constraint_bits
    for f,r in k.rules:
        if r[0]=='op':raw=desired.get(r[1],0)//r[2]
        elif r[0]=='attr':raw=desired.get((r[1],r[2]),0)
        elif r[0]=='table':
            if r[1]=='IDENTICAL':raw=desired[r[2][0][1]]
            else:
                candidates=[]
                for raw,rows in pydecode.table_inverse(arch,r[1]).items():
                    for row in rows:
                        if all(a[0]!='const' or a[1]==v for a,v in zip(r[2],row)):
                            mismatch=sum(desired.get(pydecode.key(a),0)!=v for a,v in zip(r[2],row) if a[0]!='const')
                            candidates.append((mismatch,raw))
                if not candidates:raise ValueError(r[1])
                raw=min(candidates)[1]
        else:continue
        word=(word&~f.mask)|f.put(raw&((1<<f.width)-1))
    data=((word&~k.constraint_mask)|k.constraint_bits).to_bytes(16,'little');decoded=pydecode.decode(archname,data)
    if decoded.klass.name!=classname:raise ValueError((classname,decoded.klass.name))
    for name,value in values.items():
        key=tuple(name.split('@')) if '@' in name else name
        if isinstance(value,str):value=arch.enums[k.operand_types[name].type][value]
        if key in decoded.env and decoded.env[key]!=value:
            if isinstance(value,int) and value<0:
                source=next((f for f,r in k.rules if r[0]=='op' and r[1]==name),None)
                if source and decoded.env[key]==value%(1<<source.width):continue
            raise ValueError((name,value,decoded.env[key]))
    return data.hex()

if __name__=='__main__':
    print(encode('SM89','mov__RI',Rd=3,Sb=5,PixMaskU04=15))
