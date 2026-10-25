"""Finds ptxas 13.0.88's Mercury handler directories: arrays of 16-byte {rows, count} entries whose
rows are 24-byte {variant key, handler, this adjust}. Usage: python3 handler_directories.py OUT.json"""
import struct, json, sys
data = open("/usr/local/cuda-13.0/bin/ptxas", "rb").read()
phoff, = struct.unpack_from("<Q", data, 0x20); phnum, = struct.unpack_from("<H", data, 0x38)
segs = [struct.unpack_from("<IIQQQQQQ", data, phoff + 56 * i) for i in range(phnum)]
def off(v):
    for t, f, so, sv, p, fs, ms, a in segs:
        if t == 1 and sv <= v < sv + fs and v + 8 <= sv + fs: return v - sv + so
def va(o):
    for t, f, so, sv, p, fs, ms, a in segs:
        if t == 1 and so <= o < so + fs: return o - so + sv
TEXT = (0x400000, 0x2200000)
def row_ok(r):
    o = off(r)
    if o is None: return False
    key, fn, adj = struct.unpack_from("<QQq", data, o)
    return key < 0x10000 and TEXT[0] <= (fn & ~1) < TEXT[1] and -0x1000 < adj < 0x1000
def entry(v):
    o = off(v)
    if o is None: return None
    rows, n = struct.unpack_from("<QQ", data, o)
    if n == 0 and rows == 0: return (0, 0)
    if 0 < n < 256 and all(row_ok(rows + 24 * j) for j in range(n)): return (rows, n)
    return None
# candidate directory starts: 8-aligned addresses in data segments where many consecutive entries are valid
dirs = []
for t, f, so, sv, p, fs, ms, a in segs:
    if t != 1: continue
    lo = max(sv, 0x2200000); v = lo - lo % 16 + 16
    while v < sv + fs - 16:
        run, k = 0, v
        while (e := entry(k)) is not None: run += e[1] > 0; k += 16
        if run >= 20: dirs.append((v, (k - v) // 16, run)); v = k
        else: v += 8
json.dump([[d, n, r] for d, n, r in dirs], open(sys.argv[1], "w"))
for d, n, r in dirs: print(f"directory {d:#x}: {n} classes, {r} non-empty")
