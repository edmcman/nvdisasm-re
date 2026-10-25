"""The scalar_semantics.cu whole-kernel fixtures: one `sem_<family>` kernel per family, compiled
once, with arithmetic fields patched where PTX lowering cannot select a SASS mode. Kernels read
three inputs per thread and write one output; families supply vectors and references."""
import random
from functools import cache
from sass.decode import ir, pydecode
from sass.assemble.encoding import klass
from tests.harness.case import encode
from tests.harness.gpu import ENCODING, Fixture, Template, compile
from sass.semantics.fp import softfloat
from tests.refs.float import underflow_band

EDGES = [0, 0xffffffff, 0x80000000, 0x7fffffff, 0x7f800000, 0xff800000, 0x7fc00000, 1, 0x007fffff, 0x00800000, 0x3f800000,
         0xbf800000, 0x7f7fffff, 0x80000001]
EDGES16 = [0, 0x8000, 1, 0x8001, 0x03ff, 0x0400, 0x3c00, 0xbc00, 0x7bff, 0xfbff, 0x7c00, 0xfc00, 0x7e01]


def vectors(family, rng=None):
    """128 random 32-bit triples (from a per-family seed) whose first 14 repeat the fp32/int edges."""
    rng = rng or random.Random(family)
    out = [[rng.getrandbits(32) for _ in range(3)] for _ in range(128)]
    out[:len(EDGES)] = [[x, x, x] for x in EDGES]
    return out


def half_edges(vs):
    """Overwrite the leading vectors with packed fp16 edges in every lane position."""
    n = len(EDGES16)
    vs[:n] = [[x | (EDGES16[(i + 3) % n] << 16), EDGES16[(i + 5) % n] | (x << 16), EDGES16[(i + 7) % n] | (EDGES16[(i + 9) % n] << 16)]
              for i, x in enumerate(EDGES16)]
    return vs


# Family -> (mnemonic to find, class to re-encode it as (None: same), field overrides).
PATCHES = {
    "hfma2_move": ("HFMA2", "hfma2_mma__RRI_norelu", dict(Ra=255, Rb=255, **{"Ra@negate": 1, "Rb@negate": 0})),
    "hadd2_lanes": ("HADD2", None, dict(iswzA="H0_H0", iswzB_as_C="H1_H1", **{"Ra@absolute": 1, "Rc@negate": 1})),
    "hfma2_lanes": ("HFMA2", None, dict(iswzA="H1_H1", iswzB="H0_H0", iswzC="H1_H0", **{"Ra@negate": 1, "Rb@absolute": 1})),
    "hmul2_lanes": ("HMUL2", None, dict(iswzA="H0_H0", iswzB="H1_H1", **{"Ra@absolute": 1, "Rb@negate": 1})),
    "hmul2_fmz": ("HMUL2", "hmul2__RR", dict(fmz="FMZ")),
    **{f: ("FMNMX", None, dict(nan="NAN")) for f in ("fmin_nan", "fmax_nan")},
    **{f: ("FMNMX", None, dict(ftz="FTZ", nan="NAN")) for f in ("fmin_ftz_nan", "fmax_ftz_nan")},
    "hmnmx2_xorsign": ("HMNMX2", "hmnmx2__RRR", dict(xorsign="XORSIGN", **{"Ra@absolute": 1, "Rb@absolute": 1})),
    "hmnmx2_xorsign_rir": ("HMNMX2", "hmnmx2__RIR", dict(xorsign="XORSIGN", Sb=0x3c00, Sb1=0x3c00, **{"Ra@absolute": 1})),
    "hmnmx2_xorsign_rir_noabs": ("HMNMX2", "hmnmx2__RIR", dict(xorsign="XORSIGN", Sb=0x3c00, Sb1=0x3c00, **{"Ra@absolute": 0})),
    **{f"hmnmx2_{p}nan_{m}": ("HMNMX2", "hmnmx2__RRR", dict(nan="NAN", **fmt)) for m in ("min", "max")
       for p, fmt in (("", {}), ("bf16_", dict(ofmt="BF16_V2")), ("e6m9_", dict(ofmt="E6M9_V2")))},
    **{f"hmnmx2_ftz_{m}": ("HMNMX2", "hmnmx2__RRR", dict(ftz="FTZ")) for m in ("min", "max")},
    **{f"hmnmx2_{p}_{m}": ("HMNMX2", "hmnmx2__RRR", dict(ofmt=fmt)) for m in ("min", "max") for p, fmt in (("bf16", "BF16_V2"), ("e6m9", "E6M9_V2"))},
    **{f"hmnmx2_{p}_xorsign": ("HMNMX2", "hmnmx2__RRR", dict(ofmt=fmt, xorsign="XORSIGN", **{"Ra@absolute": 1, "Rb@absolute": 1}))
       for p, fmt in (("bf16", "BF16_V2"), ("e6m9", "E6M9_V2"))},
    "i2ip_s8_nosat": ("I2IP", "i2ip__RRR_RRR", dict(satrelu="nosatrelu")),
    "i2ip_s8_satrelu": ("I2IP", "i2ip__RRR_RRR", dict(satrelu="SATRELU")),
    "i2ip_s4_satrelu": ("I2IP", "i2ip_24__RRR_RRR", dict(satrelu="SATRELU")),
    "imad_hi_c": ("IMAD", "imad_hi__RRC_RRC", dict(Sc_bank=0, Sc_addr=0x170)),
    **{f: ("F2I", "f2i__Rb_32b", {"Rb@absolute": a, "Rb@negate": n}) for f, a, n in (("f2i_abs", 1, 0), ("f2i_neg", 0, 1), ("f2i_absneg", 1, 1))},
    **{f"f2fp_{fmt.lower()}_pack{s}": ("F2FP", "f2fp_f32_to_8b_downconvert__RRR", dict(dstfmt=fmt, srcfmt="F32", merge="PACK_AB_MERGE_C", rndMode="RN",
       satfinite="nosatfinite" if s else "SATFINITE", relu="norelu", extract="H1")) for fmt in ("E4M3", "E5M2") for s in ("", "_nosat")},
    **{f"vabsdiff4_{f}_{r}": ("LOP3", "vabsdiff4__RRR_RRR", dict(Pu="PT", fmt=f.upper(), red=red)) for f in ("s8", "u8")
       for r, red in (("merge", "SIMD_MRG"), ("acc", "ACC"))},
    **{"hfma2_move_" + s: ("HFMA2", "hfma2_mma__RRI_norelu", dict(Ra=255, Rb=255, Sc=bits >> 16, Sc1=bits & 0xffff, **{"Ra@negate": 1, "Rb@negate": 0}))
       for s, bits in (("zero", 0x80000000), ("nan", 0xfe01fd01), ("denorm", 0x000103ff), ("inf", 0x7c00fc00))}}
SET_COMPARISONS = ("LT", "EQ", "LE", "GT", "NE", "GE", "NUM", "NAN", "LTU", "EQU", "LEU", "GTU", "NEU", "GEU", "T", "F")
SET_LANES = {"iswzA": "H1_H1", "iswzB_as_C": "H0_H0", "Ra@negate": 1, "Rc@absolute": 1}
# FSET/HSET2/HSETP2 family -> (mnemonic, patched fields) over `cmp.AND P` carriers (.BF for HSET2).
SETS = {**{f"{op}_{suffix}": (op.upper(), {"fcomp" if op == "fset" else "cmp": cmp, **extra}) for op in ("fset", "hset2", "hsetp2")
           for suffix, cmp, extra in [*((c.lower(), c, {}) for c in SET_COMPARISONS), ("or", "GEU", dict(bop="OR")), ("xor", "LT", dict(bop="XOR")),
                                      ("ftz", "LE", dict(ftz="FTZ")), ("notp", "LT", {"Pp@not": 1})]},
        "hset2_bm": ("HSET2", dict(cmp="LT", bval="BM")), "hset2_bm_geu": ("HSET2", dict(cmp="GEU", bval="BM")),
        "hset2_lanes": ("HSET2", dict(cmp="LT", **SET_LANES)), "hsetp2_lanes": ("HSETP2", dict(cmp="LT", **SET_LANES)),
        **{f"hsetp2_hand{s}": ("HSETP2", dict(cmp=cmp, h_and="H_AND", **extra))
           for s, cmp, extra in (("", "LT", {}), ("_or", "LE", dict(bop="OR")), ("_xor", "LT", dict(bop="XOR")), ("_nan", "NAN", {}))}}
PATCHES.update({f: (m, None, fields) for f, (m, fields) in SETS.items()})
# FP8 unpack kernels are compiled without RELU; both conversions get the RELU bit (75) set.
RELU_UNPACK = tuple(f"f2fp_{f}_unpack_{h}_relu" for f in ("e4m3", "e5m2") for h in ("h0", "h1"))


@cache
def template(arch="SM89"):
    """The compiled, patched scalar_semantics cubin."""
    image = compile("scalar_semantics", "-diag-suppress=177", arch=arch)
    out = bytearray(image)
    for family, (mnemonic, cls, fields) in PATCHES.items(): out = _patch(out, image, family, mnemonic, cls, fields, arch=arch)
    for family in RELU_UNPACK:
        t = Template(bytes(out), "sem_" + family)
        for offset in _find(t, "F2FP", family, 2):
            d = t.decoded(offset)
            values = dict(operands(d), relu='RELU')
            replacement = int.from_bytes(bytes.fromhex(encode(arch, d.klass.name, **values)), 'little')
            out = bytearray(t.patch({offset: (replacement, t.control(offset))}, bytes(out)))
    return bytes(out)


def _find(t, mnemonic, family, count=1):
    matches = [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == mnemonic and
               (family != "imad_hi_c" or t.decoded(o).klass.name == "imad_hi__RRR_RRR")]
    if len(matches) != count: raise ValueError(f"{family}: expected {count} {mnemonic} instructions, got {len(matches)}")
    return matches


def operands(d, k=None):
    """A decoded instruction's operand values, for those operands class k (default its own) has."""
    names = (k or d.klass).operand_types
    return {**{n: v for n, v in d.env.items() if isinstance(n, str) and n in names},
            **{"@".join(n): v for n, v in d.env.items() if isinstance(n, tuple) and len(n) == 2 and n[0] in names}}


def _patch(out, image, family, mnemonic, cls, fields, arch="SM89"):
    """Re-encode the family's one target instruction with `fields`, keeping its controls."""
    t = Template(image, "sem_" + family)
    if arch == 'SM90':
        if family.startswith('hfma2_move'): cls = 'hfma2_mma__RRI'
        if family in ('hadd2_lanes', 'hfma2_lanes', 'hmul2_lanes', 'hmul2_fmz'):
            mnemonic = 'HFMA2.MMA'
            cls = {'hadd2_lanes': 'hadd2__RR', 'hfma2_lanes': 'hfma2__RRR',
                   'hmul2_lanes': 'hmul2__RR', 'hmul2_fmz': 'hmul2__RR'}[family]
    offset, = _find(t, mnemonic, family)
    d = t.decoded(offset)
    fields = dict(fields)
    if family == 'imad_hi_c':
        from sass.assemble import elf
        fields['Sc_addr'] = elf.PROFILES[arch].parameter_base + 16
    values = {**operands(d, klass(arch, cls or d.klass.name)), **fields}
    words = {}
    if family.startswith("f2fp_e") and "_pack" in family:
        # The compiler's third input is the last LDG before this F2FP; hardware carries the selected
        # C half when Rd aliases Rc.
        loads = [t.decoded(o).env["Rd"] for o in range(0, offset, 16) if t.decoded(o).klass.mnemonic.startswith("LDG") and "Rd" in t.decoded(o).env]
        if len(loads) < 3: raise ValueError(f"{family}: expected three input loads")
        values["Rc"] = values["Rd"] = loads[-1]
        # The PTX kernel extracts the 16-bit result and rebuilds the upper half from C; route
        # both half masks from F2FP's full result instead, exposing PACK_AB_MERGE_C.
        masks = [o for o in range(offset + 16, len(t.code), 16) if t.decoded(o).klass.mnemonic == "LOP3" and t.decoded(o).env.get("Sb") in (0xffff0000, 0xffff)]
        if len(masks) != 2: raise ValueError(f"{family}: expected two output half masks, got {len(masks)}")
        for o in masks:
            m = t.decoded(o); ops = operands(m)
            ops["Ra" if m.env["Sb"] == 0xffff0000 else "Rc"] = values["Rd"]
            words[o] = (int.from_bytes(bytes.fromhex(encode(arch, m.klass.name, **ops)), "little"), t.control(o))
    words[offset] = (int.from_bytes(bytes.fromhex(encode(arch, cls or d.klass.name, **values)), "little"), t.control(offset))
    return bytearray(t.patch(words, bytes(out)))


def fixture(family, vs, expected=None, targets=(), wide=False, wide_output=None, arguments=(), arch="SM89", **kw):
    """A sem_<family> launch over vectors `vs`; wide: 64-bit inputs and outputs."""
    import struct
    data = b"".join(struct.pack("<QQQ" if wide else "<III", *v) for v in vs)
    return Fixture(name=family, image=template(arch=arch), kernel="sem_" + family, data=data, expected=expected, input_size=8 if wide else 4,
                   output_size=8 if wide or wide_output else 4, arguments=arguments, targets=targets, vectors=vs, **kw)


TARGETS = {
    "iadd3": ("IADD3",), "imad": ("IMAD",), "imad_hi": ("IMAD.HI",), "imad_hi_c": ("IMAD.HI",),
    **{f"idp2a_{m}_{a}{b}": (f'IDP.2A.{m.upper()}.{"S16" if a == "s" else "U16"}.{"S8" if b == "s" else "U8"}',) for m in ("lo", "hi") for a in "su" for b in "su"},
    "lop3": ("LOP3",), "shf": ("SHF",), "iabs": ("IABS",), "imnmx": ("IMNMX",),
    **{f"vabsdiff4_{f}_{r}": ("VABSDIFF4",) for f in ("s8", "u8") for r in ("merge", "acc")},
    "dadd": ("DADD",), "dmul": ("DMUL",), "dfma": ("DFMA",), "dsetp": ("DSETP",),
    "hadd2": ("HADD2",), "hfma2": ("HFMA2",), "hmul2": ("HMUL2",), "hmul2_sat": ("HMUL2.SAT",), "hmul2_ftz": ("HMUL2.FTZ",), "hmul2_fmz": ("HMUL2.FMZ",),
    "hadd2_sat": ("HADD2.SAT",), "hfma2_sat": ("HFMA2.SAT",), **{f: ("HFMA2.MMA",) for f in ("hfma2_move", "hfma2_move_zero", "hfma2_move_nan", "hfma2_move_denorm", "hfma2_move_inf")},
    "hadd2_lanes": ("HADD2",), "hfma2_lanes": ("HFMA2",), "hmul2_lanes": ("HMUL2",),
    "prmt": ("PRMT",), "brev": ("BREV",), "popc": ("POPC",), "bfind": ("FLO.U32",), "bfind_shift": ("FLO.U32.SH",), "bfind_s32": ("FLO",), "bfind_shift_s32": ("FLO.SH",),
    "fadd": ("FADD",), "fmul": ("FMUL",), "ffma": ("FFMA",),
    "isetp": ("ISETP",), "isetp_ex": ("ISETP.GE.U32.AND.EX",), "sel": ("ISETP",), "fsetp": ("FSETP",),
    **{f: ("F2I",) for f in ("f2i", "f2i_abs", "f2i_neg", "f2i_absneg")}, "f2ip": ("F2IP",),
    **{f"i2ip_{f}": (f"I2IP.{f.upper()}",) for f in ("s2", "u2", "s4", "u4", "s8", "u8", "s16", "u16")},
    "i2ip_s8_nosat": ("I2IP.S8",), "i2ip_s8_satrelu": ("I2IP.S8",), "i2ip_s4_satrelu": ("I2IP.S4",),
    "fsel": ("FSEL",), "fmin": ("FMNMX",), "fmax": ("FMNMX",), "fmin_xorsign": ("FMNMX.XORSIGN",),
    "fmin_nan": ("FMNMX.NAN",), "fmax_nan": ("FMNMX.NAN",), "fmin_ftz_nan": ("FMNMX.FTZ.NAN",), "fmax_ftz_nan": ("FMNMX.FTZ.NAN",),
    "fmin_ftz": ("FMNMX.FTZ",), "fmax_ftz": ("FMNMX.FTZ",),
    "hmnmx2_min": ("HMNMX2",), "hmnmx2_max": ("HMNMX2",), "hmnmx2_xorsign": ("HMNMX2.XORSIGN",),
    "hmnmx2_xorsign_rir": ("HMNMX2.XORSIGN",), "hmnmx2_xorsign_rir_noabs": ("HMNMX2.XORSIGN",),
    "hmnmx2_nan_min": ("HMNMX2.NAN",), "hmnmx2_nan_max": ("HMNMX2.NAN",), "hmnmx2_ftz_min": ("HMNMX2.FTZ",), "hmnmx2_ftz_max": ("HMNMX2.FTZ",),
    "hmnmx2_bf16_min": ("HMNMX2.BF16_V2",), "hmnmx2_bf16_max": ("HMNMX2.BF16_V2",),
    "hmnmx2_bf16_nan_min": ("HMNMX2.BF16_V2.NAN",), "hmnmx2_bf16_nan_max": ("HMNMX2.BF16_V2.NAN",), "hmnmx2_bf16_xorsign": ("HMNMX2.BF16_V2.XORSIGN",),
    "hmnmx2_e6m9_min": ("HMNMX2.E6M9_V2",), "hmnmx2_e6m9_max": ("HMNMX2.E6M9_V2",),
    "hmnmx2_e6m9_nan_min": ("HMNMX2.E6M9_V2.NAN",), "hmnmx2_e6m9_nan_max": ("HMNMX2.E6M9_V2.NAN",), "hmnmx2_e6m9_xorsign": ("HMNMX2.E6M9_V2.XORSIGN",),
    "i2f": ("I2FP",), "fadd_ftz": ("FADD.FTZ",), "ffma_sat": ("FFMA.SAT",), "shared": ("LDS", "STS"), "local": ("LDL", "STL"), "generic": ("LD", "ST"), "desc": ("LDG", "STG", "LD", "ST"), "pnz": ("LD",), "desc_param": ("LDG", "STG"),
    "branch": ("BRA", "BSSY", "BSYNC"), "ldc": ("LDC",),
    **{f"{op}_{m}": (f"{op.upper()}.{m.upper()}",) for op in ("fadd", "fmul", "ffma", "dadd", "dmul", "dfma") for m in ("rz", "rm", "rp")},
    "f2fp_f16_pack_rn": ("F2FP.F16",), "f2fp_f16_pack_rz": ("F2FP.F16",), "f2fp_bf16_pack_rn": ("F2FP.BF16",), "f2fp_bf16_pack_rz": ("F2FP.BF16",),
    "f2fp_f16_pack_imm": ("F2FP.F16",), "f2fp_f16_pack_c": ("F2FP.F16",), "f2fp_f16_pack_relu": ("F2FP.RELU.F16",),
    "f2fp_f16_pack_satfinite": ("F2FP.SATFINITE.F16",), "f2fp_f16_pack_relu_satfinite": ("F2FP.SATFINITE.RELU.F16",),
    "f2fp_bf16_pack_relu": ("F2FP.RELU.BF16",), "f2fp_bf16_pack_satfinite": ("F2FP.SATFINITE.BF16",), "f2fp_bf16_pack_relu_satfinite": ("F2FP.SATFINITE.RELU.BF16",),
    "f2fp_e4m3_pack": ("F2FP.SATFINITE.E4M3",), "f2fp_e5m2_pack": ("F2FP.SATFINITE.E5M2",),
    **{f"f2fp_{f}_unpack_{h}{r}": (f"F2FP.{'RELU.' if r else ''}F16.{f.upper()}.UNPACK_B",) for f in ("e4m3", "e5m2") for h in ("h0", "h1") for r in ("", "_relu")},
    "f2fp_e4m3_pack_nosat": ("F2FP.E4M3",), "f2fp_e5m2_pack_nosat": ("F2FP.E5M2",)}
TARGETS.update({f: (m,) for f, (m, _) in SETS.items()})
ROUNDED = {f"{op}_{m}" for op in ("fadd", "fmul", "ffma", "dadd", "dmul", "dfma") for m in ("rz", "rm", "rp")}  # directed-rounding kernels
HALF_OUTPUTS = {"hadd2", "hfma2", "hmul2", "hmul2_sat", "hmul2_ftz", "hmul2_fmz", "hadd2_sat", "hfma2_sat", "hadd2_lanes", "hfma2_lanes", "hmul2_lanes"}
WIDE = {"dadd", "dmul", "dfma", "dsetp", "isetp_ex"} | {f for f in ROUNDED if f.startswith("d")}


def scalar_vectors(family):
    """The family's 128 input triples (edge rows first)."""
    if family == "imad_hi_c": vs = vectors(family, random.Random(0x1a2ad))
    elif family.startswith("idp2a_"): vs = vectors(family, random.Random(sum((i + 1) * ord(ch) for i, ch in enumerate(family))))
    else: vs = vectors(family)
    match family:
        case f if f.startswith("vabsdiff4_"):
            vs[:8] = [(0, 0xffffffff, 0xdeadbeef), (0x807f01ff, 0xff010080, 0x7f80ff01), (0x80808080, 0x7f7f7f7f, 0x01020304), (0xffffffff, 0, 0x80000000),
                      (0x7f000180, 0x80ff017f, 0xffffffff), (0x01020304, 0x05060708, 0), (0x80000000, 0x7fffffff, 0x7f80ff01), (0xdeadbeef, 0x12345678, 0xabcdef01)]
        case f if f in SETS:
            # Pairs of edges (equal pairs first), per lane for the packed forms; Pp from c.
            values = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x3f800000, 0xbf800000, 0x40000000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f7fffff] if f.startswith("fset") \
                else [0, 0x8000, 1, 0x8001, 0x03ff, 0x3c00, 0xbc00, 0x4000, 0x7c00, 0xfc00, 0x7e00, 0x7bff]
            n, rng = len(values), random.Random(f)
            pair = lambda i: (values[i % n], values[(i + i // n) % n])
            if f.startswith("fset"): vs = [[*pair(i), rng.getrandbits(1)] for i in range(128)]
            else: vs = [[lo[0] | hi[0] << 16, lo[1] | hi[1] << 16, rng.getrandbits(1)] for i in range(128) for lo, hi in [(pair(i), pair(i * 7 + 3))]]
        case "imad_hi_c":
            vs[:8] = [(0xffffffff, 2, 0), (0x80000000, 2, 0), (0xffffffff, 0xffffffff, 0), (0x80000001, 0xffffffff, 0), (0x7fffffff, 0x7fffffff, 0),
                      (0x12345678, 0x87654321, 0), (0x40000000, 4, 0), (0xfffffffe, 3, 0)]
        case f if f.startswith("idp2a_"):
            vs[:8] = [(0x80017fff, 0x807f01ff, 0x7fffffff), (0x7fff8001, 0x01020304, 0x80000000), (0xffff0001, 0x807f80ff, 0xffffffff), (0x00010001, 0xffffffff, 0),
                      (0x80008000, 0x7f807f80, 1), (0x7fff7fff, 0x008000ff, 0xfffffffe), (0xffff7fff, 0x80ff00ff, 0x12345678), (0x01020304, 0x05060708, 0x87654321)]
        case "fsel" | "fmin" | "fmax" | "fmin_nan" | "fmax_nan" | "fmin_ftz_nan" | "fmax_ftz_nan" | "fmin_ftz" | "fmax_ftz" | "fmin_xorsign":
            pairs = [(0, 0x80000000), (0x80000000, 0), (0x7fc00000, 0x3f800000), (0x3f800000, 0x7fc00000), (0x7f800001, 0xff800001), (1, 0), (0x80000001, 0), (0x007fffff, 0x80000001)]
            vs[len(EDGES):len(EDGES) + 2 * len(pairs)] = [[x, y, p] for x, y in pairs for p in (0, 1)]
        case f if f.startswith("hmnmx2_"):
            if f.startswith("hmnmx2_bf16"):
                pairs = [(0x3f803f80, 0xc000c000), (0, 0x80008000), (0x7fc17f80, 0x40003f80), (0x00010001, 0x80018001), (0x7f80ff80, 0x7f7fff7f), (0xbf803f80, 0x4200c000), (0xffc17fc1, 0x7fc17fc1)]
            elif f.startswith("hmnmx2_e6m9"):
                pairs = [(0x40004000, 0x3f803f80), (0, 0x80008000), (0x7fff7fff, 0x3f803f80), (0x7e00fe00, 0x3f80bf80), (0x00010001, 0x80028002), (0xc0003f80, 0x41003e00), (0xffff7fff, 0x7fff7fff)]
            elif f.startswith("hmnmx2_xorsign_rir"):
                pairs = [(0xc000c000, 0), (0x40004000, 0), (0x3c003c00, 0), (0xbc00bc00, 0), (0xc0003c00, 0), (0x3c00c000, 0)]
            else:
                pairs = [(0x3c003c00, 0xc000c000), (0, 0x80008000), (0x7e017c00, 0x40003c00), (0x00010001, 0x80018001), (0x7c00fc00, 0x7bfffbff), (0xbc003c00, 0x4200c000), (0xffc17fc1, 0x7fc17fc1)]
            pairs += {"hmnmx2_xorsign": [(0x80017e01, 0x7e017e01), (0x7e017e01, 0x80017e01)], "hmnmx2_bf16_xorsign": [(0x80010000, 0x7fc17fc1), (0x7fc17fc1, 0x80010000)],
                      "hmnmx2_e6m9_xorsign": [(0x80010000, 0x7e017e01), (0x7e017e01, 0x80010000)]}.get(f, [])
            vs = [[a, b, 0] for a, b in (pairs * (128 // len(pairs) + 1))[:128]]
    if family in HALF_OUTPUTS or family == "hfma2_move": half_edges(vs)
    if family.startswith("hmul2"):
        pairs = [(0x00010001, 0x60006000), (0x80018001, 0x60006000), (0x00010001, 0xe000e000), (0x80018001, 0xe000e000), (0x14001400, 0x14001400), (0x94009400, 0x14001400),
                 (0x94009400, 0x94009400), (0x40004000, 0x40004000), (0xbc00bc00, 0x40004000), (0x7e017c00, 0x3c003c00), (0x7c007e01, 0x3c003c00), (0x7c007c00, 0)]
        vs[:len(pairs)] = [[x, y, 0] for x, y in pairs]
    if family == "f2ip":
        values = [0, 0x80000000, 0x3f000000, 0xbf000000, 0x3fc00000, 0xbfc00000, 0x40200000, 0xc0200000, 0x42ff0000, 0xc3000000, 0x43000000, 0xc2ff0000,
                  0x42ff8000, 0xc2ff8000, 0x437f8000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 0x4f000000, 0xcf000000, 0x7f7fffff, 0xff7fffff]
        rng = random.Random(family)
        for _ in range(128 * 3): rng.getrandbits(32)  # continue the family stream after its base vectors
        vs = [[values[i % len(values)], values[(i + 7) % len(values)], rng.getrandbits(32)] for i in range(128)]
    if family.startswith(("f2fp_f16_pack_", "f2fp_bf16_pack_")):
        values = [0, 0x80000000, 0x3f800000, 0xbf800000, 0x3f801000, 0x3f803000, 0x33000000, 0x33800000, 0x387fc000, 0x38800000, 0x477fe000, 0x477ff000,
                  0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 1, 0x007fffff, 0x00800000, 0x7f7fffff, 0xff7fffff]
        vs = [[values[i % len(values)], values[(i * 7 + 3) % len(values)], 0] for i in range(128)]
    if family.startswith(("f2fp_e4m3_pack", "f2fp_e5m2_pack")):
        values = [0, 0x80000000, 1, 0x007fffff, 0x00800000, 0x33800000, 0x3f800000, 0xbf800000, 0x3f801000, 0x3f803000, 0x40600000, 0x43e00000, 0x43e10000,
                  0x43e08000, 0x477fe000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001,
                  0x43e80000, 0xc3e80000, 0x47700000, 0xc7700000]  # RN overflow ties: E4M3 +-464, E5M2 +-61440
        vs = [[values[i % len(values)], values[(i * 7 + 3) % len(values)], (0x13579bdf ^ (i * 0x01010101)) & 0xffffffff] for i in range(128)]
    if family.startswith(("f2fp_e4m3_unpack_", "f2fp_e5m2_unpack_")):
        vs = [[(i | ((255 - i) << 8)) << (16 if "_h1" in family else 0), 0, 0] for i in range(128)]
    if family.startswith("i2ip_"):
        fmt = family.split("_")[1]; width, signed = int(fmt[1:]), fmt[0] == "s"
        lo, hi = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
        edges = [x & 0xffffffff for x in (0, 1, 0xffffffff, lo - 1, lo, hi, hi + 1, 0x12345678, 0x89abcdef)]
        vs = [[edges[i % len(edges)], edges[(i + 4) % len(edges)], 0x89abcdef ^ (i * 0x01010101)] for i in range(128)]
    if family in WIDE:
        rng = random.Random(family)
        for _ in range(128 * 3): rng.getrandbits(32)
        vs = [[rng.getrandbits(64) for _ in range(3)] for _ in range(128)]
        edges64 = [0, 1 << 63, 1, (1 << 63) | 1, 0x000fffffffffffff, 0x0010000000000000, 0x3ff0000000000000, 0xbff0000000000000, 0x7fefffffffffffff,
                   0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000]
        vs[:len(edges64)] = [[x, x, x] for x in edges64]
        pairs = [(0, 1 << 63, 0), (1 << 63, 0, 0), (0x7ff8000000000000, 0, 0), (0, 0x7ff0000000000001, 0),
                 (0x3ff0000000000001, 0x3feffffffffffffe, 0xbff0000000000000), (0x7fefffffffffffff, 0x4000000000000000, 0xffefffffffffffff)]
        vs[len(edges64):len(edges64) + len(pairs)] = pairs
        if family == "isetp_ex":
            vs[:10] = [(0, 1, 0), (1, 0, 0), (0, 0, 0), (1, 1, 0), (0x00000001ffffffff, 0x0000000200000000, 0), (0x0000000200000000, 0x00000001ffffffff, 0),
                       (0x7fffffffffffffff, 0x8000000000000000, 0), (0x8000000000000000, 0x7fffffffffffffff, 0), (0xffffffffffffffff, 0, 0), (0, 0xffffffffffffffff, 0)]
    if family in ROUNDED:
        # Residuals below the wide format's precision, exact cancellation (zero sign) and overflow.
        wide = family in WIDE
        one, tiny, top = (0x3ff0000000000000, 1, 0x7fefffffffffffff) if wide else (0x3f800000, 1, 0x7f7fffff)
        sign = 1 << (63 if wide else 31)
        vs[64:72] = [list(v) for v in [(one, tiny, one | sign), (one, tiny | sign, tiny), (one, one | sign, one), (one | sign, one, tiny | sign),
                                       (top, top, top | sign), (top | sign, top, top), (one + 1, one + 1, tiny | sign), (0, sign, 0)]]
    return vs


def half_underflow(emulated, want):
    """Per fp16 lane: equal, or the emulator's RN underflow signature (a signed zero where the
    reference has the same-sign minimum subnormal; see refs.float.underflow_band)."""
    return all(e == w or e & 0x7fff == 0 and w == e | 1 for e, w in (((emulated >> s) & 0xffff, (want >> s) & 0xffff) for s in (0, 16)))


def underflow_bug(family, inputs, emulated, want):
    """The emulator's RN minimum-subnormal underflow (refs.float.underflow_band): it returns a
    signed zero where the reference (and the GPU) has the signed minimum subnormal."""
    width = 64 if family.startswith("d") else 32
    op = "add" if family.endswith("add") else "mul" if family.endswith("mul") else "fma"
    if not underflow_band(op, inputs, width): return False  # all three inputs must be finite
    parts = [softfloat.unpack(x, width)[2] for x in inputs]
    exact = parts[0] + parts[1] if op == "add" else parts[0] * parts[1] + (parts[2] if op == "fma" else 0)
    sign = (1 << (width - 1)) if exact < 0 else 0
    return emulated == sign and want == sign | 1


def launch(family, reference=None, same=None, underflow=False, arch="SM89"):
    """Fixture for scalar family `family`; reference(x, y, z) gives the expected output;
    underflow excuses the emulator's known minimum-subnormal defect."""
    vs = scalar_vectors(family)
    emulator_same = (lambda e, w: e == w or half_underflow(e, w)) if family in HALF_OUTPUTS else (lambda e, w: e == w)
    targets = TARGETS[family]
    if arch == 'SM90':
        replacements = {'IMNMX': 'VIMNMX.U32', 'HADD2': 'HFMA2.MMA', 'HMUL2': 'HFMA2.MMA',
                        'HMUL2.SAT': 'HFMA2.MMA.SAT', 'HADD2.SAT': 'HFMA2.MMA.SAT', 'HFMA2.SAT': 'HFMA2.MMA.SAT',
                        'HMUL2.FTZ': 'HFMA2.MMA.FTZ'}
        if family in ('imnmx','hadd2','hmul2','hmul2_sat','hadd2_sat','hfma2_sat','hmul2_ftz'):
            targets = tuple(replacements[t] for t in targets)
    return fixture(family, vs, [reference(*v) for v in vs] if reference else None, targets=targets, wide=family in WIDE,
                   wide_output=family.startswith(("f2fp_e4m3_unpack_", "f2fp_e5m2_unpack_")),
                   arguments=((8, 0x00000002ffffffff),) if family == "imad_hi_c" else ((4, 0x3fc00000),) if family == "f2fp_f16_pack_c" else (),
                   same=same or (lambda g, w: g == w), emulator_same=emulator_same,
                   excused=(lambda i, e, w: underflow_bug(family, vs[i], e, w)) if underflow else (lambda i, e, w: False), arch=arch)

