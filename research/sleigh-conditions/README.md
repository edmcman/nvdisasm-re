# Enforcing md CONDITIONS in the generated SLEIGH (shelved 2026-10-12)

nvdisasm prints md-illegal encodings; pydecode rejects them by default (`decode(..., conditions=False)`
opts out). Making SLEIGH reject them too cost +2.3 MB `.sla` on SM89 exact at best (SM90 exact
reached 18.1 MB, over Ghidra's 16 MiB limit), so it's shelved until the semantics shrink.

- `gen_sleigh_conditions.patch`: generator changes against `sass/gen_sleigh.py` at that time
  (per-condition decision subtables, bit-cube covers, catch-all + dead-end tables).
- `mdcond_legal_classes.py`: `legal_classes`, partitioning each encoding field's raw values by the
  condition subexpressions that read only it.

Verified SLEIGH behaviour (Ghidra 12.1.4, toy language):
- A subtable with one constructor is folded into its parent as a full pattern; an OR inside it
  becomes DNF in every referencing root (identical-pattern errors, compiler OOM).
- A word fails to decode when an operand subtable has no matching constructor. A catch-all plus a
  more specific constructor leading to a dead-end table rejects exactly that pattern; the dead end
  needs two constructors flipping two fixed bits (contradictory patterns are compile errors).
- `attach names` with `_` rejects a value at decode when the field is displayed.

Cost is per referenced subtable (~800 bytes), not per reference.
