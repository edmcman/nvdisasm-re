# Manual GPU probe audit — 2026-10-29

Scope: discovery scripts and input files under `research/`, plus the maintained
fixture entry points under `tests/cases/`, checked against the adapters in
`tests/harness/autocase_*.py`. Historical raw
observations remain evidence of what executed; changing their driver does not
create reference verification credit.

| Probe | Classification and next action |
| --- | --- |
| This sass2cu investigation | The standalone predicate/conversion matrix and its runner were removed. The maintained integer, floating, packed-half and conversion adapters now own the inputs, output aliases and references. Use `tests/test_cases_on_gpu.py`; keep the initial raw records for provenance. |
| `research/fmz_xorsign/probe.py` | Single-instruction matrix that should use autocases. `autocase_extended.py` already covers FMUL/FFMA.FMZ and FMNMX.XORSIGN. Its dense zero/Inf/NaN factors and seeded triples should become profile supplements, then the ordinary GPU runner can replace the manual requests. |
| `research/mufu/sincos_reduction.py` | Single-instruction SIN/COS discovery that should use autocases. `autocase_remaining.py` owns these operations and `mufu_sincos` supplies the reference corpus. Reduction-boundary inputs should extend that profile; raw re-observation of adapter candidates remains appropriate for unknown results. |
| `research/mufu/probe.py`, `survey.py`, `fp16.py`, `segment_fit.py`, `sin_fit.py`, `structure.py`, `sin_structure.py`, `absolute_error.py`, `by_exponent.py` | These use a patched kernel as a transport for one MUFU instruction. The analysis remains useful, but GPU input generation should use or extend the MUFU adapter. F16 and unresolved operations need discovery support in that adapter. Large exhaustive domains need a streamed corpus. |
| `research/bra_div/probe.py` | The instruction and its warp context fit the shared probe runner. Extend `autocase_system.py`'s branch adapter with the divergence reference and active/waiting-lane profiles. The ordinary branch adapter currently lacks those contexts. |
| `research/ld32.py` | Most sizes, offsets, aliases and predicate behavior belong in the memory profiles. Explicit low virtual-address placement and overflow/underflow controls require extending the memory setup adapter; document that setup limitation rather than duplicating ordinary cases. |
| `research/ldg-predicate-probes.json`, `research/ld32/window-probes-SM89.json` | Manual single-instruction input files. Ordinary memory/predicate scenarios should extend the memory adapter. Synthetic address windows need an explicit setup contract; windows that cannot be placed on the GPU must remain emulator-only. |
| `research/popc_inc/probe.py` | Concrete harness limitation: shared-memory initialization, synchronization, selected/exit lanes, repeated atomics and per-lane capture are combined by a custom kernel. Existing memory and warp adapters do not express this lifecycle. Treat it as a multi-instruction cooperative probe until a combined adapter exists. |
| `research/fp_backends_cuda.py`, `fp_backends_host.py` | Backend arithmetic/performance experiments, not SASS single-instruction probes. Their compiled comparison kernels are appropriate. |
| `research/ptxas/*`, `research/sm107/probe.py` | Compiler tracing and PTX-to-SASS investigations; compilation/finalizer capture is their subject. Keep compiler probes. Functional verification of emitted instructions belongs in autocases. |
| Single-target `scalar.launch` fixtures in `tests/cases/int_alu.py`, `fp32.py`, `fp64.py`, `fp16.py`, `conversions.py` | Ordinary arithmetic, SETP, selectors and conversion matrices should obtain new instruction discoveries and coverage from their existing adapters. Retain current fixtures as compiled whole-kernel regression coverage; move any missing numerical/profile scenarios to adapters before extending a patched single-target matrix. A compiled wrapper alone does not justify bypassing autocases. |
| `tests/cases/carry.py`, `bmsk.py`, `memory.py`, `mufu.py` | Check each fixture's actual subject: chained carry/comparison lowering, indexed mask construction, memory addressing/lifecycle and compiler-generated MUFU selection sequences can justify whole-kernel fixtures. Direct isolated targets and patched modes belong in the matching adapter; shared memory, synchronization and repeated effects need an explicit setup contract. |

The unrelated probes above were audited, not rewritten as part of the sass2cu
requests. Their migration candidates are explicit so future investigations do
not reuse a manual matrix merely because a historical script exists.

The request 5 follow-up also moved the previous manual SM75 HADD2 F32-source
numerical corpus into `half_f32_sources` and the packed-half adapter. The new
SM75 matrix is a compiler-lowering investigation; it launches no GPU kernels.
Its instruction-level functional cases use autocases.
