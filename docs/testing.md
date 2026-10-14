# Testing notes

See `tests/README.md` for the layers, commands, options, prerequisites, ratchets and how to write cases and GPU fixtures (never pytest-xdist).

- `sass.ghidra_bridge` is the only path to `ghidra_emulate.py`/`ghidra_decode.py`; Ghidra layers run all selected languages concurrently, and layer 4 (`emulate_sharded`) also splits each language's batch into chunks so up to 24 JVMs stay busy. `conftest.require_languages` fails on a stale `.sla` instead of letting concurrent Ghidra processes recompile it.
- I2IP sub-byte forms (S2/U2 `.28`, S4/U4 `.24`) fill the high bits from C unshifted; only H1 selects C's high half (SM89 hardware, 2026-10-12; the earlier reference and p-code shifted for every non-H0 form).
- FLO.S32 (plain `FLO`; ptxas emits it for `bfind.s32`) searches the complement of a negative source: the highest bit differing from the sign, so -1 gives -1 like 0. FLO.U32 searches the value (SM89 hardware, 2026-10-12, fixtures `bfind_s32`/`bfind_shift_s32`; the earlier emitter and reference treated S32 like U32).
- Op-count samples (`synth.synthetic(..., legal=True)`) are md-legal words: up to 256 redraws use aligned low registers (UR0-UR63 on SM100+ are the only in-range uniform registers in 8-bit fields) or the zero register (HADD2.F32 requires `-RZ`).
