"""Instruction semantics: the semantics families (src/sass/semantics/families) behind decode-time selector guards,
with the opaque call for the encodings they don't model. This module does not equate PTX syntax with
undocumented SASS variants."""
from sass.semantics.family import FAMILIES, family_for
import sass.semantics.families  # registers the semantics families

OPERATION_FAMILIES = set(FAMILIES)
# Opcodes with a family that Ghidra's languages don't lower, so Ghidra decodes them as opaque (the
# lifter still models them), and the architectures where (None: every one). Ghidra 12.1 silently
# truncates a decompressed .sla past 16 MiB (`SlaFormat.MAX_FILE_SIZE`, docs/sleigh.md; `build.LIMIT`
# enforces it): generic ATOM's per-operation bodies take SM89 past it, and SM90/SM107, within 0.5 MB
# of it before atomics, have no room for the others. Later Ghidra versions drop the limit: with one,
# empty this.
GHIDRA_NOT_LOWERED = {"ATOM": None, **{op: {"SM90", "SM107"} for op in ("ATOMG", "ATOMS", "RED", "REDG")}}


def ghidra_lowers(arch, opcode):
    """Whether arch's language lowers opcode's family (it has one, and isn't left out)."""
    where = GHIDRA_NOT_LOWERED.get(opcode, ())
    return opcode in FAMILIES and where is not None and arch not in where


def ghidra_family(arch, k):
    """The family arch's language lowers class k with, or None."""
    return family_for(k) if ghidra_lowers(arch, k.mnemonic) else None




def dispatch(b, refs=()):
    """b.body (a family's native code, with its pattern references `refs`) behind b's selector
    patterns, and the opaque call for the other encodings."""
    from sass.sleigh.gen import pattern
    native = " ".join(b.body)
    fallback_lines = b.call(); fallback_refs = list(dict.fromkeys(b.call_refs))
    # The calculation is its own table, shared by every guard pattern.
    calculation = b.g.subtable("native", [('""', pattern([], refs), "", native)])
    constructors = [('""', root, "", f"build {calculation};") for clauses in (b.selector_patterns or [[]])
                    if (root := pattern(clauses, [("sub", calculation)]))]
    if b.selector_patterns or not constructors:
        constructors.append(('""', pattern([], fallback_refs), "", " ".join(fallback_lines)))
    table = b.g.subtable("dispatch", constructors)
    b.body = [f"build {table};"]; b.refs = [("sub", table)]


def emit(b):
    if not family_for(b.k): return 'opaque', 'outside implemented operation families'
    if not ghidra_lowers(b.g.arch.name, b.k.mnemonic): return 'opaque', 'not lowered for Ghidra (16 MiB .sla limit)'
    from sass.sleigh import lower
    return lower.emit(b)
