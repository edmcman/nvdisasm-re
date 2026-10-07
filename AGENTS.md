# nvdisasm reverse engineering

Target: `nvdisasm` from CUDA 13.4 (V13.4.92, built 2026-09-01), stripped x86-64 PIE.
Local copy used so far: `/tmp/.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm`.
All addresses below are specific to that build.

## Tooling
- Ghidra via GhidraMCP. GUI server was at `http://127.0.0.1:33363/` (`set_ghidra_server`). Image base 0x100000, so Ghidra addr = file/objdump addr + 0x100000.
- GhidraMCP lookups need exact function entry addresses; inner addresses return "No function".
- `extract_md.py` (`uv run extract_md.py <nvdisasm> [outdir]`) dumps the embedded machine descriptions to `out/` (gitignored: NVIDIA-derived data, do not commit).

## Findings
- Opcode/operand name strings are ROT13 (`rot13_decode` 0x168cb0); `strings` won't find them. Mnemonics are matched by prefix (`str_after_prefix` 0x11831b).
- Diagnostics: `diag_report` 0x111b70 (variadic). Messages are 16-byte `diag_msg {severity, suppressed, fmt}` records reached via a pointer slot: code -> slot -> record -> fmt.
- Branch-stack pass (pre-Volta SSY/PBK/PCNT/PRET/... stack), driver `bstack_analyze_function` 0x1da320: entry frame per block, one ordered pass over CFG edges (`bstack_propagate_edge`), then join/exit checks and pop-target resolution (`resolve_branch_target` 0x1d11b0, emits `TARGET=` annotations). Push/pop kind pairs: 1 RET/PRET, 2 SYNC/SSY, 3 BRK/PBK, 4 CONT/PCNT, 5 LONGJMP/PLONGJMP, 7 EXIT/PEXIT.
- Machine descriptions: per arch (SM75 80 86 89 90 100 101 103 120) three blobs in `.data` — md, latencies, patterns — registered by `md_register_embedded_blob` 0x1be200, loaded by `md_load_blob` 0x1beb30.
  - Cipher `mdcipher_decrypt` 0x173030: glibc-LCG keystream (4 bytes/step) + inverse sbox at 0x1fa5e0, ciphertext-chained, seeded by a 16-bit per-arch key.
  - md blobs are then LZ4 stream-compressed chunks `{u32 raw, u32 comp, data}` with 64 KB dictionary; latencies/patterns are encrypted only.
  - External override files `md_SMxx` / `latencies_SMxx` / `patterns_SMxx` are supported (`md_register_override_file` 0x1be3f0); a `_` suffix means key 0 (plaintext) — untested.
- Semantics model: md gives per-CLASS FORMAT (operand names), CONDITIONS, PROPERTIES (INSTRUCTION_TYPE...), PREDICATES (IDEST_SIZE/ISRC_*_SIZE in bits), OPCODES, ENCODING. latencies gives connectors (Ra/Rb/Rc/Re read, Rd/Rd2 written; URx, Px analogues), TABLE_TRUE/OUTPUT/ANTI latencies and OPERATION SETS (pipes). Register span = ceil(SIZE/32). No functional (value) semantics exist.

## Open
- Trace the `-plr` liveness code that consumes connectors (start at the "Cannot %s as flow analysis is disabled" check, Ghidra 0x10a327).
- Parse md + latencies into a per-opcode table (operands, role, width, pipe, latency).
