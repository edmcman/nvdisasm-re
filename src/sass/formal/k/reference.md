# Reading the definition

This document defines the executable bitvector IR used by nvdisasm-re's SASS
lifter. It includes every handwritten rule in the three modules:
`SASS-BITS`, `SASS-FLOAT`, and `SASS`.

Each module's Markdown file is also its executable K source. K extracts the
fenced `k` blocks and compiles them directly. The mathematical expressions and
prose explain those rules; the blocks are authoritative. This publication
assembles the same files, without copying the rules into a second definition.
K's builtin integers, Booleans, strings, lists, maps, and term equality supply
the imported data domains.

## K notation

| Notation | Meaning |
| :--- | :--- |
| `rule L => R` | Rewrite a matching left side to the right side |
| `requires C` | Apply the rule only when condition `C` holds |
| `<cell> ... </cell>` | Match or update one named part of the state |
| `...` inside a cell | Leave the unmentioned cell contents unconstrained |
| `A ~> B` | Evaluate computation `A` before continuation `B` |
| `.K`, `.List`, `.Map` | Empty computation, list, or map |
| `ListItem(V)` | A single list element |
| `K |-> V` | A map entry |
| `M[K <- V]` | Replace a map entry |
| `_` | An anonymous pattern variable |
| `[function]` | A term evaluated by defining function rules |
| `[owise]` | A fallback when the other applicable function rules do not match |

Suffixes such as `+Int`, `<Int`, and `==String` identify operations in K's
builtin domains. Uppercase names are variables. The exporter validates IR
arities, widths, register locations, and supported operators before constructing
a term for execution.

## Relationship to SASS and verification

The existing lifter determines which IR expressions implement an instruction.
Named operators expand through their existing definitions before export.
The model specifies how that IR executes; it does not independently define
NVIDIA's instruction set or prove the lifter correct.

Runtime primitives and effects cross an explicit environment boundary. Concrete
execution needs a response transcript; symbolic proofs need explicitly stated
environment assumptions if they use primitives. This document supplies neither
GPU approximation axioms nor a warp scheduler or concurrent memory model.

The regression suite compares K execution with the existing evaluator and
samples shared instruction cases. Four example symbolic claims cover false
predication, modular addition, simultaneous aliased reads, and a branching
program. Those claims are properties of this model. Hardware verification
continues to depend on independent references and GPU observations.
