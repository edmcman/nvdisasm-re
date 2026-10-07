#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["lz4"]
# ///
"""Extract the embedded, encrypted machine-description blobs from nvdisasm."""
import re, struct, subprocess, sys, pathlib
import lz4.block

KINDS = ["md", "latencies", "patterns"]
KEYS = {"SM75": 0xb4a, "SM80": 0x8416, "SM86": 0x8416, "SM89": 0x1684, "SM90": 0x3927}
DEFAULT_KEY = 0x9327  # SM100, SM101, SM103, SM107, SM120
INV_SBOX_OFF = 0xfa5e0
REGISTER_FN = 0xbe200            # md_register_embedded_blob
REGISTRATIONS = range(0xbd9e0, 0xbde60)  # md_register_SMxx callers
DATA_VADDR, DATA_OFF = 0x348000, 0x148000

def cstr(binary, off): return binary[off:binary.index(b"\0", off)].decode()

def registrations(binary, nvdisasm):
    dis = subprocess.run(
        ["objdump", "-d", "--no-show-raw-insn", "-M", "intel",
         f"--start-address={REGISTRATIONS.start:#x}", f"--stop-address={REGISTRATIONS.stop:#x}", nvdisasm],
        check=True, capture_output=True, text=True).stdout
    regs, cur = [], {}
    for line in dis.splitlines():
        match line:
            case _ if m := re.search(r"lea\s+r8,.*# ([0-9a-f]+)", line): cur["addr"] = int(m[1], 16)
            case _ if m := re.search(r"lea\s+rsi,.*# ([0-9a-f]+)", line): cur["arch"] = cstr(binary, int(m[1], 16))
            case _ if m := re.search(r"mov\s+r9d,0x([0-9a-f]+)", line): cur["len"] = int(m[1], 16)
            case _ if re.search(r"xor\s+ecx,ecx", line): cur["kind"] = 0
            case _ if m := re.search(r"mov\s+ecx,0x([0-9a-f]+)", line): cur["kind"] = int(m[1], 16)
            case _ if re.search(rf"(call|jmp)\s+{REGISTER_FN:x}", line): regs.append(cur); cur = {}
    return regs

def decrypt(buf, key, inv_sbox):
    seed, ks, ctr, prev = key, 0, 1, ~key & 0xff
    out = bytearray(len(buf))
    for i, c in enumerate(buf):
        ctr -= 1
        if ctr == 0:
            ctr, seed = 4, (seed * 0x41c64e6d + 0x3039) & 0xffffffff
            ks = seed
        else:
            ks >>= 8
        out[i] = inv_sbox[prev ^ c] ^ (ks & 0xff)
        prev = c
    return bytes(out)

def lz4_chunks(buf):
    out, pos = bytearray(), 0
    while pos + 8 <= len(buf):
        raw, comp = struct.unpack_from("<II", buf, pos)
        pos += 8
        if raw == 0 or pos + comp > len(buf): break
        out += lz4.block.decompress(buf[pos:pos + comp], uncompressed_size=raw, dict=bytes(out[-0x10000:]))
        pos += comp
    return bytes(out), pos

def is_text(b): return b.isascii() and b.decode().strip().isprintable()

def main(nvdisasm, outdir="out"):
    binary = pathlib.Path(nvdisasm).read_bytes()
    out = pathlib.Path(outdir); out.mkdir(exist_ok=True)
    inv_sbox = binary[INV_SBOX_OFF:INV_SBOX_OFF + 256]
    for r in registrations(binary, nvdisasm):
        off = r["addr"] - DATA_VADDR + DATA_OFF
        dec = decrypt(binary[off:off + r["len"]], KEYS.get(r["arch"], DEFAULT_KEY), inv_sbox)
        plain, used = (dec, len(dec)) if is_text(dec[:8]) else lz4_chunks(dec)
        name = f"{KINDS[r['kind']]}_{r['arch']}.txt"
        (out / name).write_bytes(plain)
        print(f"{name:24} blob={r['len']:#9x} consumed={used:#9x} -> {len(plain):9} bytes")
        if used != r["len"]: print(f"  warning: {name} not fully consumed", file=sys.stderr)

if __name__ == "__main__":
    main(*sys.argv[1:])
