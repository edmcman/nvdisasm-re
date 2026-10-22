"""Instruction semantics: the semantics families (src/sass/semantics/families) behind decode-time selector guards,
with the opaque call for the encodings they don't model. This module does not equate PTX syntax with
undocumented SASS variants."""
from sass.semantics.family import FAMILIES, family_for
import sass.semantics.families  # registers the semantics families

OPERATION_FAMILIES = set(FAMILIES)


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
    from sass.sleigh import lower
    return lower.emit(b)
