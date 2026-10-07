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
