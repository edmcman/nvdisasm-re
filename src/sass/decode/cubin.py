"""Minimal CUDA ELF (cubin) reader: sections, target SM, and the code of each kernel."""
import struct

def is_elf(data): return data[:4] == b"\x7fELF"

def sections(data):
    shoff, = struct.unpack_from("<Q", data, 0x28)
    entsize, num, strndx = struct.unpack_from("<HHH", data, 0x3a)
    hdrs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entsize) for i in range(num)]
    strtab = hdrs[strndx][4]
    name = lambda o: data[strtab + o:data.index(b"\0", strtab + o)].decode()
    return {name(h[0]): data[h[4]:h[4] + h[5]] for h in hdrs if h[1] != 8}  # skip NOBITS

def elf_arch(data):
    """Target from e_flags: low byte in ELF ABI v7, bits 8-15 from v8."""
    flags, = struct.unpack_from("<I", data, 0x30)
    sm = (flags >> 8 if data[8] >= 8 else flags) & 0xff
    return f"SM{sm}"

def text_sections(data):
    return {name: body for name, body in sections(data).items() if name.startswith(".text.")}

def words(body):
    return [body[i:i + 16] for i in range(0, len(body) - 15, 16)]

def text_words(data):
    for body in text_sections(data).values(): yield from words(body)

def info_records(body):
    """CUDA ELF attribute records (inline byte/halfword or length-prefixed data)."""
    pos=0
    while pos<len(body):
        if pos+4>len(body):raise ValueError('truncated CUDA attribute header')
        fmt,attribute,value=struct.unpack_from('<BBH',body,pos);pos+=4
        if fmt==4:
            if pos+value>len(body):raise ValueError('truncated CUDA attribute payload')
            payload=body[pos:pos+value];pos+=value
        elif fmt in (1,2,3):payload=value.to_bytes(2,'little')
        else:raise ValueError(f'unknown CUDA attribute format {fmt}')
        yield attribute,payload

def kernel_image(data,name):
    """Kernel text, constants, and parameter layout from its actual cubin metadata."""
    image=sections(data);text=image['.text.'+name];params={};base=None;size=None
    for attribute,payload in info_records(image['.nv.info.'+name]):
        if attribute==0x0a:_,base,size=struct.unpack('<IHH',payload)
        elif attribute==0x17:
            _,ordinal,offset,flags=struct.unpack('<IHHI',payload)
            params[ordinal]=dict(offset=offset,size=flags>>18)
    if base is None or size is None:raise ValueError('kernel has no parameter-bank metadata')
    if set(params)!=set(range(len(params))):raise ValueError('noncontiguous parameter ordinals')
    if any(p['offset']+p['size']>size for p in params.values()):raise ValueError('parameter outside declared bank range')
    constants={}
    for section,body in image.items():
        if not section.startswith('.nv.constant'):continue
        suffix=section[len('.nv.constant'):];bank,_,owner=suffix.partition('.')
        if not owner or owner==name:constants[int(bank)]=body
    return dict(name=name,arch=elf_arch(data),code=text,constants=constants,
                parameter_base=base,parameter_size=size,parameters=[params[i] for i in range(len(params))])
