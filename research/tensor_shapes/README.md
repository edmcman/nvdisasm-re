# Tensor shapes

Infer conditional tensor argument dimensions from SASS using structural type
unification and a common SSA equality/inequality analysis. The recorded corpus
contains 12 SM90 entries and 28 tensor arguments. See the
[HTML report](../../docs/shapes.html) for results, constraint rules, and examples.

## Run

From the worktree root, with Python 3.13 and `uv`:

```sh
nice uv run --no-project research/tensor_shapes/experiment.py \
  /path/to/culifter/test/cuda_handwritten research/tensor_shapes/common_results
python research/tensor_shapes/render_common_report.py \
  research/tensor_shapes/common_results/manifest.json docs/shapes.html
```

The runner installs its declared dependencies, runs the current tests, analyzes
every SM90 entry, and writes results and source hashes. It needs neither LLVM,
Loopy, Claripy, Crab, IKOS, Ghidra, nor a GPU. Allow roughly 15 minutes on the
recorded machine. `common_results/tests.txt` updates during the test phase.

For tests alone, set `SHAPE_CORPUS` to that corpus directory and run:

```sh
SHAPE_CORPUS=/path/to/culifter/test/cuda_handwritten \
PYTHONPATH=src:research/tensor_shapes nice uv run --no-project \
  --python 3.13 --with lz4 --with z3-solver==5.3.3 \
  python -m unittest discover -s research/tensor_shapes
```

## Files

- `infer_types.py`, `type_system.py`, `call_graph.py`, `memory_types.py`: SSA,
  structural types, local calls, and shared-memory type constraints.
- `access_relations.py`: shared modular polynomial normalization.
- `common_constraints.py`, `constraint_domain.py`, `constraint_state.py`:
  assignments, guards, joins, loop iteration counts, and fixed-point verification.
- `linear_algebra.py`, `linear_bounds.py`, `polynomial_bounds.py`: solver support.
- `word_ranges.py`, `scalar_bounds.py`, `numeric_relations.py`: integer ranges,
  congruences, and numerical error constraints.
- `common_shapes.py`: tensor dimensions from bounded element indices.
- `experiment.py`, `type_suite.py`: reproduction and corpus runners.
- `render_common_report.py`, `constraint_rules.html`: the HTML report and rules.
- `experiment_contexts.json`: explicit input ranges used by the corpus run.
- `test_*.py`, `common_results/`: current regression tests and recorded evidence.

Dimensions remain conditional on the integer interpretation of machine arithmetic
and the recorded input contexts. DOT also assumes the stated reciprocal accuracy.
Overflow, address wrapping, source rank, and allocation capacity are not certified.
