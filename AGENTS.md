# nvdisasm reverse engineering

Target: `nvdisasm` from CUDA 13.4 (V13.4.92, built 2026-09-01), stripped x86-64 PIE.
Local copy used so far: `/tmp/.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm`
(`sass.paths.NVDISASM` also looks in uv's cache). All addresses in `docs/` are specific to that build.

## Layout
`uv sync` installs the `sass` package (`src/sass`, editable) and its console scripts.
- `src/sass/md/`: NVIDIA's machine descriptions as shipped: `extract` (decrypt from nvdisasm into `out/raw/`, gitignored), `layout` (split into the committed `out/SMxx/`), `parse` (classes/enums/tables), `fixes` (known md errors, applied by `parse.load_classes`), `cond`/`expr` (CONDITIONS and expressions).
- `src/sass/decode/`: decode IR (`ir`), reference decoder (`pydecode`), encoder (`pyencode`), cubin ELF reader (`cubin`).
- `src/sass/semantics/`: our value semantics (the md has none): bitvector IR (`bitvec`), family framework (`family`), families (`families/`), `lift`/`evaluate` (Ghidra-free), `operands`, `operations` (routing), `fp/` (`fpformat`, `softfloat`), `runtime` (test-only CPU primitives).
- `src/sass/sleigh/`: SLEIGH generator (`gen`), semantics lowering (`lower`), language build (`build`), coverage summary (`coverage`).
- `src/sass/ghidra_bridge/`: programs run under Ghidra's pyghidra venv (`ghidra_decode`, `ghidra_emulate`, `BlockEmulator.java`) and `client`, the only path to them.
- `src/sass/oracle/`: nvdisasm oracle (`nvdisasm`, sqlite cache in `.cache/`), word corpora (`corpus`, `synth`).
- `src/sass/check/`: comparison/report CLIs (`pydecode_disasm`, `ghidra_disasm`, `census`).
- `src/sass/paths.py`: repository paths and external tool locations.
- `tests/`: test layers only (`harness/`, `cases/`, `refs/`, kernels, baselines); see `tests/README.md`.
- `processor/SASS/`: the Ghidra module (hand-written `.sinc`/`.pspec`/`.cspec`, generated languages, coverage/verification JSON).
- `research/`: one-off investigations (ptxas, CuLifter, SLEIGH conditions).

Console scripts: `sass-extract-md`, `sass-layout-md`, `sass-decode`, `sass-gen-sleigh`, `sass-build-languages`, `sass-coverage`, `sass-compare-pydecode`, `sass-compare-ghidra`, `sass-census`. Tests: `uv run pytest` (see `tests/README.md` for the Ghidra and GPU layers).

## Findings (docs/)
- `docs/nvdisasm-binary.md`: GhidraMCP setup, function addresses, cipher, md blobs and aliases.
- `docs/decoding.md`: md matching, oracle behavior, printing rules.
- `docs/sleigh.md`: SLEIGH generator constraints and Ghidra limits/bugs.
- `docs/semantics.md`: families, operand values, exact/readable floating point, generator reference.
- `docs/hardware-facts.md`: GPU oracle and SM89 hardware-established behavior.
- `docs/testing.md`: test-harness notes.
- `docs/open.md`: open questions and roadmap.

Record new findings in the matching `docs/` file, dated where it matters.

## Rules
- Don't name a repo dir `ghidra/` (or a module directory on `sys.path` `ghidra`/`pyghidra`): Python treats it as a namespace package shadowing pyghidra's Java `ghidra` package (RecursionError).
- SLEIGH compiler memory: compiling the SM100+ languages took 10-12 GiB of Java heap each, and `sass-build-languages` once ran all 18 compilers at once (no `MAXMEM` in `support/sleigh`, so each JVM may take a quarter of RAM); eight together exhausted 93 GiB and systemd-oomd killed whole terminals (2026-10-13). `sass-build-languages` now runs `MemAvailable // 16 GiB` compilers with `-Xmx16g` (`COMPILER_HEAP`). The test layers are small by comparison (one emulation JVM ~0.7 GB on SM90).
- Complete SLEIGH generation/compilation before parallel language loading. Ghidra auto-compiles stale specs, and concurrent compilers writing one `.sla` can corrupt it.
- Run language builds and GPU validation outside the sandbox. The user explicitly requested this; Ghidra's JDK/config setup and GPU/device access may fail inside the sandbox.
- Architecture support and hardware verification are separate. Lack of hardware evidence beyond SM89 limits verification claims, not code generation: implement semantics for other architectures whenever the machine description and available evidence make the behavior reasonable, using conservative selector guards and fallback paths for unresolved forms. Record hardware verification by architecture and tested forms rather than gating native generation to SM89.
- Treat IEEE rounding as shared floating-point machinery, not an F2FP-only feature: the exact-rounding strategy can apply to arithmetic operations such as FADD/FMUL/FFMA as well as conversions. Reuse or factor the rounding implementation where appropriate, while preserving each operation's rules for fusion, FTZ/FMZ, signed zero, saturation, and exceptional values.
