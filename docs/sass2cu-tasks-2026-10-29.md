# sass2cu GPU requests — 2026-10-29

- [x] Establish SM89 GPU access using checksum-verified local driver libraries.
- [x] Extend autocase adapters/profiles for aliased SETP outputs, F2F.FTZ.F64.F32,
  narrow F2I and exact half NaNs; implement independently established semantics.
- [x] Run adapter-generated GPU observations and reference-backed regression cases.
- [x] Add compiled double fmin/fmax kernel fixtures and exact-bit references.
- [x] Verify affected Python, native and SLEIGH/Ghidra paths.
- [x] Refresh generated plans and coverage/evidence artifacts.
- [x] Record measured behavior, SM75 inference, unresolved forms and a dated local reply.
- [x] Audit manual GPU probes for single-instruction scenarios that should use
  autocases. Migrate this investigation; document unrelated candidates separately.

Request 5 follow-up:

- [x] Seek fresh SM75 compiler evidence with runtime inputs, conversion rounding
  controls, every source position, observable packed lanes and arithmetic controls.
- [x] Preserve the 262-entry compiler matrix and add stable PTX regression fixtures.
- [x] Extend the packed-half adapter and independent reference for compiler-backed
  HSET2/HSETP2 F32 sources; move the previous manual HADD2 corpus into profiles.
- [x] Validate the expanded cases in Python, native execution and Ghidra; rerun
  affected SM89 shared GPU cases and review operation-count changes.
- [x] Trace the live ptxas optimizer and legacy half encoder; preserve paired
  HSET2/HMUL2 promotion captures, validate captured words against their cubin,
  and name, annotate, save and export the established functions in Ghidra.
- [x] Refresh plans and coverage/evidence, record compiler-backed versus inferred
  behavior and remaining opaque forms, and update the untracked local reply.

Follow-up verification: 230 unit/compiler tests, 693 Python tests (48,440
shared cases), 690 native tests, 690 Ghidra tests (48,449 cases), and 8,484
SM89 GPU scenarios passed. The final checks reproduced all ten plans,
rejudged the five affected families' recorded SM89 outputs, and passed nine
coverage/dataflow/operation-count checks. Compiler capture validation matched
34 half words exactly, including control bits; this is encoding evidence only.
Compact traces and checksums remain beside the SM75 compiler evidence. Full
logs, Ghidra exports and duplicate artifacts are preserved locally at
`.cache/pr106-research-archive-2026-10-29/`, outside the PR.

No SM75 GPU evidence was created. General mixed HADD2 source precision remains
inferred; HMUL2/HFMA2 F32 sources and nonzero HADD2.F32 sums remain opaque.
The current compiler patterns and internal eligibility policy do not establish
their rounding point.

Default-rounding compiler follow-up:

- [x] Isolate explicit `.rn` from implicit RN using live compiler traces.
- [x] Find mixed HADD2/HMUL2 and contracted HFMA2 emission in each source
  position; preserve the 114-entry matrix and twelve stable controls.
- [x] Add structural compiler regressions and validate all 30 newly captured
  words against their cubin without masking.
- [x] Save the corrected promotion policy and conversion-helper roles in Ghidra.
- [x] Record default versus explicit rounding as a required compiler-search
  comparison in the investigation skill.
- [x] Refresh coverage/evidence for the new PTX fixture and update the PR.

The default-rounding patterns permit contraction. They correct the earlier
negative emission finding but do not settle exact hardware rounding; the
precision guards remain in place.
Validation passed 194 focused checks, including 27 compiler regressions,
plus the exact-word capture check and coverage consistency. The SM75 refresh
processed 68,536 reference cases. Normalized evidence comparison confirms
that only compiler-emission forms changed (HADD2 4→7, HMUL2 2→6, HFMA2 2→5);
reference and GPU evidence are unchanged, with no SM75 GPU claim.
