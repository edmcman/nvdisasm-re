# Compiled double min/max evidence — 2026-10-29

Initial synthetic single-instruction discovery records are preserved locally
at `.cache/pr106-research-archive-2026-10-29/`. They are excluded from the PR:
their findings now have independent references and autocase regressions.
The initial 1,912 conversion and 18,350 predicate observations earn no
verification credit.

The initial standalone drivers were removed after correcting the workflow:
single-instruction discovery and regressions use maintained autocase adapters,
adding or extending an adapter as needed. References and profiles live in
`tests/refs/` and `tests/harness/`; reference-backed GPU captures are recorded in
`tests/baseline/gpu_observations/SM89.jsonl` by the normal test runner.

`double-minmax-observations.json` preserves 64 runtime input pairs per kernel,
their raw result bits and the device. `double_minmax.cubin` and
`double-minmax.sass` preserve the actual compiled image and disassembly.
`tests/kernels/double_minmax.cu` and `tests/cases/fp64.py` are the maintained
compiler-generated whole-kernel fixtures, compiled at `-O3` without fast math.
The sequence spans DSETP, FSEL, SEL and LOP3.

`metadata.json` pins compiler/tool hashes and versions, the base revision,
driver package/checksum, vector counts and compiler flags. System driver packages
and the kernel were not changed. GPU commands prepend
`.cache/nvidia-580.178.04/root/usr/lib/x86_64-linux-gnu` to `LD_LIBRARY_PATH`.
