# The nvdisasm binary

Target: `nvdisasm` from CUDA 13.4 (V13.4.92, built 2026-09-01), stripped x86-64 PIE.
`uv sync` installs it (`nvidia-cuda-nvdisasm==13.4.92`) as `.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm`; `sass.paths.NVDISASM` locates it.
All addresses below are specific to that build and are Ghidra addresses (objdump + 0x100000) unless noted; `src/sass/md/extract.py` uses objdump addresses.

## Ghidra (GhidraMCP)
- The binary was analysed in the Ghidra GUI through GhidraMCP; the server was at `http://127.0.0.1:33363/` (`set_ghidra_server`). Image base 0x100000, so Ghidra addr = file/objdump addr + 0x100000.
- GhidraMCP lookups need exact function entry addresses; inner addresses return "No function".

## Findings
- Opcode/operand name strings are ROT13 (`rot13_decode` 0x168cb0); `strings` won't find them. Mnemonics are matched by prefix (`str_after_prefix` 0x11831b).
- Diagnostics: `diag_report` 0x111b70 (variadic). Messages are 16-byte `diag_msg {severity, suppressed, fmt}` records reached via a pointer slot: code -> slot -> record -> fmt.
- Branch-stack pass (pre-Volta SSY/PBK/PCNT/PRET/... stack), driver `bstack_analyze_function` 0x1da320: entry frame per block, one ordered pass over CFG edges (`bstack_propagate_edge`), then join/exit checks and pop-target resolution (`resolve_branch_target` 0x1d11b0, emits `TARGET=` annotations). It runs by default to infer indirect-branch targets; `--print-life-ranges` builds on the same framework, using the connector def/use data. Push/pop kind pairs: 1 RET/PRET, 2 SYNC/SSY, 3 BRK/PBK, 4 CONT/PCNT, 5 LONGJMP/PLONGJMP, 7 EXIT/PEXIT.
- Machine descriptions ([machine-descriptions.md](machine-descriptions.md) covers their content and extraction): per arch (SM75 80 86 89 90 100 101 103 107 120) three blobs in `.data` — md, latencies, patterns — registered by `md_register_embedded_blob` 0x1be200, loaded by `md_load_blob` 0x1beb30.
  - Cipher `mdcipher_decrypt` 0x173030: glibc-LCG keystream (4 bytes/step) + inverse sbox at 0x1fa5e0, ciphertext-chained, seeded by a 16-bit per-arch key.
  - md blobs are then LZ4 stream-compressed chunks `{u32 raw, u32 comp, data}` with 64 KB dictionary; latencies/patterns are encrypted only.
  - SM107 also has its own md/latencies/patterns (registered at objdump 0xbde60 = Ghidra 0x1bde60, just past the other nine; `src/sass/md/extract.py` missed it until 2026-10-13). It is a distinct ISA (SM103 plus SM120's ALU forms and 56 new classes; `docs/semantics.md`), supported like the others since 2026-10-14.
  - `-b` accepts more names than there are mds: `arch_family_name` 0x1bb0c0 maps the SM number to the md name passed to `md_load_blob`. SM87/SM88 -> SM86, SM110 -> SM101 (CUDA 13 renamed Thor sm_101 to sm_110), SM121 -> SM120; `a`/`f` suffixes are ignored. Verified by identical nvdisasm output on synthetic words, so the aliases need no separate tables or languages. `cubin.elf_arch` applies them (`cubin.ALIASES`), so SM87/SM88/SM110/SM121 cubins decode as nvdisasm does.
  - External override files `md_SMxx` / `latencies_SMxx` / `patterns_SMxx` are supported (`md_register_override_file` 0x1be3f0); a `_` suffix means key 0 (plaintext) — untested.
- Semantics model: md gives per-CLASS FORMAT (operand names), CONDITIONS, PROPERTIES (INSTRUCTION_TYPE...), PREDICATES (IDEST_SIZE/ISRC_*_SIZE in bits), OPCODES, ENCODING. latencies gives connectors (Ra/Rb/Rc/Re read, Rd/Rd2 written; URx, Px analogues), TABLE_TRUE/OUTPUT/ANTI latencies and OPERATION SETS (pipes). Register span = ceil(SIZE/32). No functional (value) semantics exist.
