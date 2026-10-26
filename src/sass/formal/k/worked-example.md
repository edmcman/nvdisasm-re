# A lifted program

This worked example follows a four-instruction SM89 program from assembly,
through lifted bitvector IR, to an executable K program. The document builder
assembles and lifts the program using the public APIs each time it runs.

## SASS input and control flow

The program starts at `0x100000`. R0 initially contains `0xffffffff`;
P0 is the input predicate. All other architectural inputs default to zero.

```text
{{assembly}}
```

The first addition wraps to zero modulo $2^{32}$. If P0 is true, the branch
skips the second addition. If P0 is false, execution falls through and adds two.
Both paths reach EXIT:

$$
\begin{aligned}
\text{P0}=0:\quad&
\mathtt{100000}\to\mathtt{100010}\to\mathtt{100020}
\to\mathtt{100030}\to 0,\\
\text{P0}=1:\quad&
\mathtt{100000}\to\mathtt{100010}\to\mathtt{100030}\to 0.
\end{aligned}
$$

Addresses in this diagram are hexadecimal. EXIT's lifted return target is
PC zero, which terminates this IR program.

## Operational proof of execution

Proof status for this publication: **{{proof_status}}**.

For each predicate input, the builder emits a reachability claim for every
instruction, a halt claim, and a claim for the complete execution.
With `--prove-example`, K checks all eleven claims against the actual
exported program and the executable IR rules. Failure stops publication.
Without that option, the status above is **not checked**; the displayed
judgments are proposed claims.

Write $C_j=\langle pc,R0,j\rangle$ for the architectural projection of an
instruction-boundary configuration. P0 is fixed to the path's input.
PC and R0 are shown in hexadecimal; the step count is decimal.
The initial configuration is ready for dispatch after `run` installs its inputs.
The claims also fix the program, empty memory and runtime inputs, running
status, and remaining budget $8-j$. Memo and pending maps are quantified
because dispatch resets them. An arrow below denotes reachability through
K's internal rewrites, rather than a new instruction-level semantic rule.
Its label identifies the exact claim in `example/claims.k`.

{{derivation}}

The first claim proves the wraparound update to R0. The branch claim proves
either skipped predication or transfer to `0x100030`. The final instruction
claim proves transfer to PC zero; the halt claim then proves termination.
The complete-execution claims also check the composed endpoints.
The proof concerns this lifted IR program and supplies no hardware evidence.

The `example/` artifacts include `claims.k` and `proof.json`; checked builds
also include the prover output in `proof.log`. Reproduce the proof with:

```sh
nice kprove .cache/formal-doc/example/claims.k \
  --definition PATH_TO_COMPILED_SASS \
  --spec-module SASS-EXECUTION-PROOF -I src/sass/formal/k
```

## The two executions as a state graph

Each box shows the PC and R0 at an instruction boundary. The two concrete
inputs share the displayed prefix, with P0 initially equal to $p$. Edge labels
identify the instruction or predicate choice. Final boxes include the executed
instruction count. The state values are generated from the reference traces.

```{=latex}
{{diagram}}
```

## Lifted bitvector IR

The following listing is generated from `lifter.lift`, including each decoded
instruction's text, guard, register writes, and flow. The destination suffix
`:32` identifies R0's width; additions wrap to that width. P0 has width one.

```text
{{lifted}}
```

R0 appears as both source and destination. The expression reads its value
before the instruction's pending write is committed. The instruction predicate
on BRA is evaluated before its body: when P0 is zero, the branch is skipped
and the fall-through PC is retained.

## Exported K program

`k.export_program("SM89", code, base=0x100000)` produces this module.
The program map uses decimal addresses: 1048576 is `0x100000`, and each
subsequent instruction adds 16. Expression ids are local to one instruction;
the memo is cleared before the next instruction.

```k
{{exported}}
```

In the first write, `node(1, "add", 32, ...)` adds a register-read node and a
constant node. `write("R", 0, 1, ...)` stages the result into one R element.
The branch instruction has a P0 register-read guard and a constant 64-bit target.
The lists of effects and stores are empty. This example uses no runtime
primitive, response transcript, or environment assumption.

## Initial K input

Compile the exported module to make `exportedProgram()` available. For P0
equal to zero, the input is:

```k
{{input}}
```

The arguments after the register map are byte memory, parameters, runtime
responses, environment, and an eight-instruction budget. The second supplied
input differs only in the P0 value.

The publication writes the assembly, instruction bytes, exported module,
manifest, and both inputs into its `example/` directory. From the checkout,
reproduce either run with:

```sh
nice kompile .cache/formal-doc/example/program.k \
  --main-module SASS-EXPORTED --syntax-module SASS-EXPORTED \
  --backend haskell --output-definition .cache/formal-example-k \
  -I src/sass/formal/k
nice krun .cache/formal-doc/example/input-p0-0.sass \
  --definition .cache/formal-example-k --output pretty
nice krun .cache/formal-doc/example/input-p0-1.sass \
  --definition .cache/formal-example-k --output pretty
```

## Execution at instruction boundaries

The tables below are generated by the reference evaluator. The K regression
test executes both inputs and checks the same final registers, PCs, and
instruction counts against the simple expected outcomes. These are checks
of lifted IR execution; they add no hardware evidence.

{{traces}}

Both runs finish with PC zero and status `halted`. P0 is preserved, and memory
is unchanged.
