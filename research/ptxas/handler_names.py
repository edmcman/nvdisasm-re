"""Names every Mercury dispatch-table handler in ptxas 13.0.88 (research/ptxas/ptxas-finalizer-notes.md,
"Handler names"). Usage: objdump -d --no-show-raw-insn -M intel ptxas > DIR/ptxas.asm, then
uv run python research/ptxas/handler_names.py DIR; writes DIR/handler_names.json."""
import json, re, struct, sys
from collections import Counter, defaultdict
from sass.decode import ir
S = sys.argv[1]
data = open("/usr/local/cuda-13.0/bin/ptxas", "rb").read()
phoff, = struct.unpack_from("<Q", data, 0x20); phnum, = struct.unpack_from("<H", data, 0x38)
segs = [struct.unpack_from("<IIQQQQQQ", data, phoff + 56 * i) for i in range(phnum)]
def off(v):
    for t, f, so, sv, p, fs, ms, a in segs:
        if t == 1 and sv <= v < sv + fs: return v - sv + so
# directory base -> (family, md arch), from gdb (one-shot breakpoints per -arch) and dispatcher adjacency
FAMILY = {0x22dac60: "sm90", 0x22e6380: "sm90", 0x23e5980: "sm90", 0x23d8dc0: "sm90", 0x23df3a0: "sm90",
          0x231de60: "sm100", 0x2325180: "sm100", 0x232e6e0: "sm100", 0x239a4a0: "sm100", 0x23a6940: "sm100",
          0x22bf1a0: "sm103", 0x22c6520: "sm103", 0x22cfae0: "sm103", 0x233b340: "sm103", 0x2347860: "sm103",
          0x237eec0: "sm110", 0x238b260: "sm110", 0x23b7e60: "sm110", 0x23be860: "sm110", 0x23c7380: "sm110",
          0x22fd8c0: "sm120", 0x2304ba0: "sm120", 0x230f560: "sm120", 0x235ca20: "sm120", 0x236b5c0: "sm120"}
MD = {"sm90": "SM90", "sm100": "SM100", "sm103": "SM103", "sm110": "SM101", "sm120": "SM120"}
asm = open(S + "/ptxas.asm").read().splitlines()
index = {l.split(":")[0].strip(): k for k, l in enumerate(asm) if re.match(r"^ +[0-9a-f]+:", l)}
mnem = {}
for a in MD.values():
    m = defaultdict(set)
    for k in ir.load(a).classes: m[k.opcode].add(k.mnemonic)
    mnem[a] = m
def body(fn):
    k = index.get(f"{fn & ~1:x}")
    if k is None: return []
    out = []
    for l in asm[k:k + 400]:
        out.append(l)
        if "\tret" in l: break
    return out
def features(fn):
    b = body(fn)
    if b and re.search(r"\tjmp +[0-9a-f]+ <", b[0]):   # thunk
        return ("thunk", int(re.search(r"\tjmp +([0-9a-f]+)", b[0]).group(1), 16))
    dec = any(re.search(r"mov +WORD PTR \[r..\+0xc\],(ax|0x[0-9a-f]+)$", l) for l in b[:16])
    refs = [int(m.group(1), 16) for l in b[:25] if (m := re.search(r"\[rip\+0x[0-9a-f]+\].*# ([0-9a-f]+)", l))]
    imm = next((int(m.group(1), 16) for l in b[:60] if (m := re.search(r"or +QWORD PTR \[r..\],(0x[0-9a-f]+)$", l))), None)
    tmpl = struct.unpack_from("<QQ", data, off(refs[0])) if refs and off(refs[0]) else (imm, 0) if imm is not None else None
    return ("decode" if dec else "code", tmpl)
rows = []
for d, fam in FAMILY.items():
    cls = 0
    while True:
        o = off(d + 16 * cls)
        r, n = struct.unpack_from("<QQ", data, o)
        if not (n == 0 and r == 0 or 0 < n < 256 and off(r)): break
        for j in range(n):
            key, fn, adj = struct.unpack_from("<QQq", data, off(r + 24 * j))
            if key >= 0x10000 or not 0x400000 <= fn < 0x2200000: break
            rows.append((d, fam, cls, key & 0xff, key >> 8 & 0xff, fn))
        cls += 1
        if cls > 400: break
stats = defaultdict(Counter)
feat = {}
for d, fam, cls, v, t, fn in rows:
    f = feat.setdefault(fn, features(fn))
    kind = f[0]
    if kind == "code":
        ok = f[1] is not None and ((f[1][0] & 0xfff) | ((f[1][1] >> 27) & 1) << 12) in mnem[MD[fam]]
        kind = "sass-template" if ok else "other"
    stats[d][kind] += 1
for d in FAMILY:
    print(f"{d:#x} {FAMILY[d]:6} {dict(stats[d])}")
json.dump(dict(rows=rows), open(S + "/rows.json", "w"))

ROLE = {}
for d, fam in FAMILY.items():
    s = stats[d]
    ROLE[d] = ("encode" if s["sass-template"] else
               "decode_intermediate" if s["decode"] and not s["other"] and any(FAMILY[e] == fam and stats[e]["other"] == s["decode"] and not stats[e]["decode"] for e in FAMILY) else
               "encode_intermediate" if s["other"] and not s["decode"] and any(FAMILY[e] == fam and stats[e]["decode"] == s["other"] and not stats[e]["other"] for e in FAMILY) else
               "aux_decode" if s["decode"] else "aux")
def opmn(fam, tmpl):
    if tmpl is None: return None
    op = (tmpl[0] & 0xfff) | ((tmpl[1] >> 27) & 1) << 12
    m = mnem[MD[fam]].get(op)
    return "_".join(sorted(m)).replace(".", "_") if m else None
final = {}
for d, fam, cls, v, t, fn in rows:
    if ROLE[d] == "encode":
        f = feat[fn]
        if f[0] == "thunk": f = feat.setdefault(f[1], features(f[1]))
        if (mn := opmn(fam, f[1])): final[(fam, cls, v, t)] = mn
byclass = defaultdict(set)
for (fam, cls, v, t), mn in final.items(): byclass[(fam, cls)].add(mn)
names = defaultdict(set)
for d, fam, cls, v, t, fn in rows:
    mn = final.get((fam, cls, v, t)) or (next(iter(byclass[(fam, cls)])) if len(byclass[(fam, cls)]) == 1 else None)
    names[fn].add((ROLE[d], fam, cls, v, t, mn))
out = {}
for fn, uses in names.items():
    shapes = {(r, c, v, t, m) for r, f, c, v, t, m in uses}
    fams = sorted({f for r, f, *_ in uses}, key=lambda f: list(MD).index(f))
    fams = ["sm90plus"] if len(fams) == len(MD) else fams
    if len(shapes) == 1:
        r, c, v, t, m = shapes.pop()
        out[fn] = f"{r}_{'_'.join(fams)}_class_{c:02x}_key_{v:02x}_{t:02x}" + (f"_{m}" if m else "")
    else:
        mns = sorted({m for *_, m in shapes if m})
        out[fn] = f"{'_'.join(sorted({r for r, *_ in shapes}))}_{'_'.join(fams)}_shared_{len(shapes)}_rows" + (f"_{'_'.join(mns)}" if len(mns) == 1 else "")
json.dump({f"{k:#x}": v for k, v in out.items()}, open(S + "/handler_names.json", "w"), indent=0)
print(len(rows), "rows,", len(out), "distinct handlers")
print(Counter(n.split("_class_")[0].split("_shared")[0] for n in out.values()).most_common(12))
for k in (0x1344d70, 0x1ae7b00, 0x1addd50, 0x1803e2c and 0x17f4ec0, 0x12b7560, 0x1a71e40, 0x1255720):
    print(hex(k), out.get(k))
print("with mnemonic:", sum(1 for n in out.values() if not re.search(r"_key_.._..$|_rows$", n)), "of", len(out))
