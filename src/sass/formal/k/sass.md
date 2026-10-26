# Instruction and program execution

`sass.md` defines `SASS`, importing exact floating point and bitvectors.
A program maps instruction addresses to lifted instructions. PC zero denotes
termination; each fall-through instruction advances PC by 16 bytes.

## IR syntax

An expression `node(ID, OP, W, ARGS)` carries a stable instruction-local id,
operator, result width, and argument list. An instruction contains its address,
optional guard, ordered effects, register writes, memory stores, and optional
flow. `absent` represents an unconditional guard; `unsupported` retains the
reason why an instruction could not be modeled.

The `run` input supplies the program, registers, byte memory, parameters,
runtime responses, environment, and instruction budget, in that order.

```k
requires "float.md"

// State transitions of the executable IR, with an explicit environment boundary.
module SASS
  imports SASS-FLOAT
  imports MAP
  imports K-EQUAL

  syntax Expr ::= node(Int, String, Int, List)
  syntax OptionalExpr ::= Expr | "absent"
  syntax Instruction ::= instruction(Int, OptionalExpr, List, List, List, OptionalFlow)
                       | unsupported(Int, OptionalExpr, String)
  syntax OptionalFlow ::= "noFlow" | flow(String, Expr, OptionalExpr)
  syntax Program ::= program(Map, Int)
  syntax Input ::= run(Program, Map, Map, Map, Map, Map, Int)
  syntax KItem ::= OptionalExpr | Instruction | OptionalFlow | Program
                 | location(String, Int) | byte(String, Int)
                 | write(String, Int, Int, Expr)
                 | store(String, Expr, Expr, OptionalExpr)
                 | effect(Int, String, List)
                 | request(Int, Int, Int)
                 | response(String, List, Int, Int, List, Map, Map)
  syntax KResult ::= Int | String | List
  syntax Map ::= "(" Map ")" [bracket]

```

## Machine configuration

The configuration separates architectural state from execution bookkeeping.
The `k` cell is the current computation; `~>` sequences its continuations.

| Cells | Responsibility |
| :--- | :--- |
| `program`, `pc`, `nextPc` | Instruction lookup and pending control transfer |
| `regs`, `memory` | Architectural register elements and bytes |
| `params` | Explicit expression inputs |
| `responses`, `environment`, `events` | Assumed runtime transcript and environment state |
| `memo` | Values already evaluated in this instruction |
| `pendingRegs`, `pendingMemory` | Writes awaiting commit |
| `step`, `remaining`, `status` | Progress, budget, and termination reason |

```k
  configuration
    <sass>
      <k> $PGM:Input </k>
      <program> .Map </program>
      <regs> .Map </regs>
      <memory> .Map </memory>
      <params> .Map </params>
      <responses> .Map </responses>
      <environment> .Map </environment>
      <events> .List </events>
      <pc> 0 </pc>
      <nextPc> 0 </nextPc>
      <step> 0 </step>
      <remaining> 0 </remaining>
      <memo> .Map </memo>
      <pendingRegs> .Map </pendingRegs>
      <pendingMemory> .Map </pendingMemory>
      <status> "running" </status>
    </sass>

```

## Continuations and state helpers

The internal constructors below describe evaluation stages. Register elements
have width 32 for R/UR/SR and width 1 for P/UP. An absent register element or
memory byte reads as zero. Wide registers and memory values are little-endian.
For example, a memory read of $n$ bytes at address $a$ denotes

$$
\operatorname{read}(M,s,a,n)=\sum_{j=0}^{n-1}M(s,a+j)\,2^{8j},
$$

with zero for missing entries. `overlay` commits a pending map over a base map.

```k
  syntax KItem ::= "next" | dispatch(Instruction) | gate(Instruction)
                 | eval(OptionalExpr) | evalArgs(List, List) | argument(List, List)
                 | remember(Int) | choose(Expr, Expr)
                 | compute(Int, String, Int, List)
                 | invoke(Int, String, Int, List)
                 | "discard"
                 | effects(List) | effectArgs(Int, String)
                 | writes(List) | writeValue(String, Int, Int)
                 | stores(List) | storeGuard(String, Expr, Expr)
                 | storeAddress(String, Expr) | storeValue(String, Int, Int)
                 | transfer(OptionalFlow) | flowGuard(Expr) | "setTarget"
                 | "commit" | "skip" | "finishStep" | fail(String)
  syntax Int ::= getInt(Map, KItem) [function]
               | elementBits(String) [function]
               | readRegs(String, Int, Int, Map) [function]
               | readBytes(String, Int, Int, Map) [function]
  syntax Map ::= writeRegs(String, Int, Int, Int, Map) [function]
               | writeBytes(String, Int, Int, Int, Map) [function]
               | overlay(Map, Map) [function]
  syntax List ::= widths(List) [function]
  syntax Int ::= widthOf(KItem) [function]
  syntax Bool ::= isFloatOp(String) [function, total]
                  | isOrdinaryOp(String) [function, total]

  rule getInt(M, K) => {M[K]}:>Int requires K in_keys(M)
  rule getInt(M, K) => 0 requires notBool (K in_keys(M))
  rule elementBits("R") => 32
  rule elementBits("UR") => 32
  rule elementBits("SR") => 32
  rule elementBits("P") => 1
  rule elementBits("UP") => 1
  rule readRegs(_, _, 0, _) => 0
  rule readRegs(F, I, 1, M) => norm(getInt(M, location(F, I)), elementBits(F))
  rule readRegs(F, I, N, M) => norm(getInt(M, location(F, I)), elementBits(F)) |Int
                             (readRegs(F, I +Int 1, N -Int 1, M) <<Int elementBits(F)) requires N >Int 1
  rule writeRegs(_, _, 0, _, M) => M
  rule writeRegs(F, I, N, V, M) => writeRegs(F, I +Int 1, N -Int 1, V >>Int elementBits(F),
                                           M[location(F, I) <- norm(V, elementBits(F))]) requires N >Int 0
  rule readBytes(_, _, 0, _) => 0
  rule readBytes(S, A, 1, M) => getInt(M, byte(S, A))
  rule readBytes(S, A, N, M) => getInt(M, byte(S, A)) |Int
                             (readBytes(S, A +Int 1, N -Int 1, M) <<Int 8) requires N >Int 1
  rule writeBytes(_, _, 0, _, M) => M
  rule writeBytes(S, A, N, V, M) => writeBytes(S, A +Int 1, N -Int 1, V >>Int 8,
                                             M[byte(S, A) <- V &Int 255]) requires N >Int 0
  rule overlay(M, .Map) => M
  rule overlay(M, (K |-> V) REST) => overlay(M[K <- V], REST)
  rule widths(.List) => .List
  rule widths(ListItem(A) REST) => ListItem(widthOf(A)) widths(REST)
  rule widthOf(node(_, _, W, _)) => W
  rule widthOf(_) => 0 [owise]
  rule isFloatOp(OP) =>
    OP ==String "fadd" orBool OP ==String "fmul" orBool OP ==String "ffma" orBool OP ==String "nanresult" orBool
    OP ==String "flt" orBool OP ==String "fle" orBool OP ==String "feq" orBool OP ==String "fnan" orBool OP ==String "fconv"
  rule isOrdinaryOp(OP) =>
    OP ==String "add" orBool OP ==String "sub" orBool OP ==String "mul" orBool OP ==String "and" orBool
    OP ==String "or" orBool OP ==String "xor" orBool OP ==String "not" orBool OP ==String "neg" orBool
    OP ==String "shl" orBool OP ==String "shr" orBool OP ==String "sar" orBool OP ==String "eq" orBool
    OP ==String "ne" orBool OP ==String "ult" orBool OP ==String "ule" orBool OP ==String "slt" orBool
    OP ==String "sle" orBool OP ==String "zext" orBool OP ==String "sext" orBool OP ==String "extract" orBool OP ==String "concat"

```

## Dispatch, predication, and termination

Dispatch resets memoized values and pending writes. A false guard skips the
instruction body, including effects and operand reads. A true guard schedules
these phases in order:

$$
\text{effects}\ \longrightarrow\ \text{register values}\
\longrightarrow\ \text{stores}\ \longrightarrow\ \text{flow}\
\longrightarrow\ \text{commit}.
$$

PC zero halts successfully. A nonzero PC with no remaining budget fails with
`instruction limit`; an absent instruction address fails with `PC outside
program`. An unsupported instruction fails only if its guard is true.

```k
  rule <k> run(program(P, ENTRY), R, M, A, RESP, ENV, LIMIT) => next ... </k>
       <program> _ => P </program> <pc> _ => ENTRY </pc>
       <regs> _ => R </regs> <memory> _ => M </memory> <params> _ => A </params>
       <responses> _ => RESP </responses> <environment> _ => ENV </environment>
       <remaining> _ => LIMIT </remaining>
  rule <k> next => .K ... </k> <pc> 0 </pc> <status> _ => "halted" </status>
  rule <k> next => fail("instruction limit") ... </k> <pc> PC </pc> <remaining> 0 </remaining>
    requires PC =/=Int 0
  rule <k> next => fail("PC outside program") ... </k> <pc> PC </pc>
       <program> P </program> <remaining> N </remaining>
    requires PC =/=Int 0 andBool N >Int 0 andBool notBool (PC in_keys(P))
  rule <k> next => dispatch({P[PC]}:>Instruction) ... </k> <pc> PC </pc> <program> P </program>
       <remaining> N </remaining> <memo> _ => .Map </memo>
       <pendingRegs> _ => .Map </pendingRegs> <pendingMemory> _ => .Map </pendingMemory>
       <nextPc> _ => PC +Int 16 </nextPc>
    requires PC =/=Int 0 andBool N >Int 0 andBool PC in_keys(P)
  rule <k> dispatch(instruction(A, G, E, W, S, F)) => eval(G) ~> gate(instruction(A, G, E, W, S, F)) ... </k>
  rule <k> dispatch(unsupported(A, G, WHY)) => eval(G) ~> gate(unsupported(A, G, WHY)) ... </k>
  rule <k> 0 ~> gate(_) => skip ... </k>
  rule <k> V:Int ~> gate(instruction(_, _, E, W, S, F))
           => effects(E) ~> writes(W) ~> stores(S) ~> transfer(F) ~> commit ... </k>
    requires V =/=Int 0
  rule <k> V:Int ~> gate(unsupported(_, _, WHY)) => fail(WHY) ... </k> requires V =/=Int 0

```

## Expression evaluation and sharing

The memoized-value rule has priority over evaluation. Each expression id is
evaluated at most once per instruction, including runtime primitives.
Conditional expressions evaluate the condition and only the chosen arm.
Other argument lists evaluate left to right. Missing parameters fail explicitly.

Register and load expressions read architectural state. Integer and
floating-point operators dispatch to the preceding modules. Primitive nodes
request an explicit environment response.

```k
  // Expr ids are stable within one instruction. The cache resets at dispatch.
  rule <k> eval(node(ID, _, _, _)) => CACHE[ID] ... </k> <memo> CACHE </memo>
    requires ID in_keys(CACHE) [priority(40)]
  rule <k> eval(absent) => 1 ... </k>
  rule <k> eval(node(ID, "ite", _, ListItem(C:Expr) ListItem(A:Expr) ListItem(B:Expr)))
           => eval(C) ~> choose(A, B) ~> remember(ID) ... </k> [priority(60)]
  rule <k> 0 ~> choose(_, B) => eval(B) ... </k>
  rule <k> V:Int ~> choose(A, _) => eval(A) ... </k> requires V =/=Int 0
  rule <k> eval(node(ID, OP, W, ARGS)) => evalArgs(ARGS, .List) ~> compute(ID, OP, W, widths(ARGS)) ... </k>
    requires OP =/=String "ite" [priority(60)]
  rule <k> evalArgs(.List, DONE) => DONE ... </k>
  rule <k> evalArgs(ListItem(E:Expr) REST, DONE) => eval(E) ~> argument(REST, DONE) ... </k>
  rule <k> evalArgs(ListItem(I:Int) REST, DONE) => evalArgs(REST, DONE ListItem(I)) ... </k>
  rule <k> evalArgs(ListItem(S:String) REST, DONE) => evalArgs(REST, DONE ListItem(S)) ... </k>
  rule <k> V:Int ~> argument(REST, DONE) => evalArgs(REST, DONE ListItem(V)) ... </k>
  rule <k> V:Int ~> remember(ID) => V ... </k> <memo> M => M[ID <- V] </memo>
  rule <k> ListItem(V:Int) ~> compute(ID, "const", W, _) => norm(V, W) ~> remember(ID) ... </k>
  rule <k> ListItem(NAME:String) ~> compute(ID, "param", W, _) => norm({P[NAME]}:>Int, W) ~> remember(ID) ... </k>
       <params> P </params> requires NAME in_keys(P)
  rule <k> ListItem(NAME:String) ~> compute(_, "param", _, _) => fail("unbound parameter") ... </k>
       <params> P </params> requires notBool (NAME in_keys(P))
  rule <k> ListItem(F:String) ListItem(I:Int) ListItem(N:Int) ~> compute(ID, "reg", _, _)
           => readRegs(F, I, N, R) ~> remember(ID) ... </k> <regs> R </regs>
  rule <k> ListItem(S:String) ListItem(A:Int) ~> compute(ID, "load", W, _)
           => readBytes(S, A, W /Int 8, M) ~> remember(ID) ... </k> <memory> M </memory>
  rule <k> ListItem(NAME:String) ARGS ~> compute(ID, "primitive", W, _)
           => invoke(ID, "sass_prim_" +String NAME, W, ARGS) ~> remember(ID) ... </k>
  rule <k> VS:List ~> compute(ID, OP, W, WS) => bv(OP, W, VS, WS) ~> remember(ID) ... </k>
    requires isOrdinaryOp(OP)
  rule <k> VS:List ~> compute(ID, OP, W, WS) => fp(OP, W, VS, WS) ~> remember(ID) ... </k>
    requires isFloatOp(OP)

```

## Explicit runtime contracts

A runtime request is indexed by $(\text{step},\text{pc},\text{id})$. Its response
contains the name, evaluated arguments, result width, result value, events,
and environment before and after the operation.

K accepts a response only if the name, arguments, width, and pre-environment
match and $0\leq v<2^w$. It then installs the post-environment and appends the
provided events. Missing and mismatched responses fail. Effects have width zero
and must return zero. Effect ids are negative; expression ids are nonnegative.

This is an explicit assumption boundary. The rules do not give a default
answer to a primitive, model GPU scheduling, or establish that a supplied
response matches hardware. Proof-specific environment rules belong in a
separate contract module with documented assumptions.

```k
  // A concrete contract instance supplies a result and an explicit environment transition.
  // No primitive receives a default answer. Proof-specific contract modules may add rules
  // for invoke, but are separate from this concrete response-transcript definition.
  rule <k> invoke(ID, NAME, W, ARGS) => V ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> response(NAME, ARGS, W, V, EV, BEFORE, AFTER)) ... </responses>
       <events> EVENTS => EVENTS EV </events> <environment> BEFORE => AFTER </environment>
    requires V >=Int 0 andBool V <Int (2 ^Int W)
  rule <k> invoke(ID, _, _, _) => fail("runtime response missing") ... </k>
       <pc> PC </pc> <step> STEP </step> <responses> RESP </responses>
    requires notBool (request(STEP, PC, ID) in_keys(RESP))
  rule <k> invoke(ID, NAME, W, ARGS) => fail("runtime response mismatch") ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> response(N, A, WIDTH, V, _, BEFORE, _)) ... </responses>
       <environment> ENV </environment>
    requires NAME =/=String N orBool ARGS =/=K A orBool W =/=Int WIDTH orBool V <Int 0 orBool V >=Int (2 ^Int W)
             orBool BEFORE =/=K ENV

```

## Effects and staged writes

Effects execute first, in order. Register writes and byte stores accumulate in
pending maps while their expressions continue to read the pre-write architectural
state. Store guards are lazy: a false guard reads neither address nor value.
Overlapping destinations use ordered, last-write-wins updates.

```k
  rule <k> effects(.List) => .K ... </k>
  rule <k> effects(ListItem(effect(ID, NAME, ARGS)) REST)
           => evalArgs(ARGS, .List) ~> effectArgs(ID, NAME) ~> discard ~> effects(REST) ... </k>
  rule <k> VS:List ~> effectArgs(ID, NAME) => invoke(ID, NAME, 0, VS) ... </k>
  rule <k> _:Int ~> discard => .K ... </k>
  rule <k> writes(.List) => .K ... </k>
  rule <k> writes(ListItem(write(F, I, N, V)) REST)
           => eval(V) ~> writeValue(F, I, N) ~> writes(REST) ... </k>
  rule <k> V:Int ~> writeValue(F, I, N) => .K ... </k>
       <pendingRegs> R => writeRegs(F, I, N, V, R) </pendingRegs>
  rule <k> stores(.List) => .K ... </k>
  rule <k> stores(ListItem(store(S, A, V, C)) REST)
           => eval(C) ~> storeGuard(S, A, V) ~> stores(REST) ... </k>
  rule <k> 0 ~> storeGuard(_, _, _) => .K ... </k>
  rule <k> C:Int ~> storeGuard(S, A, V) => eval(A) ~> storeAddress(S, V) ... </k> requires C =/=Int 0
  rule <k> A:Int ~> storeAddress(S, V:Expr) => eval(V) ~> storeValue(S, A, widthOf(V)) ... </k>
  rule <k> V:Int ~> storeValue(S, A, W) => .K ... </k>
       <pendingMemory> M => writeBytes(S, A, W /Int 8, V, M) </pendingMemory>
```

## Control transfer and commit

Flow evaluates its guard before its target. A false flow guard keeps the
fall-through PC. Calls and returns use the lifted target directly; this model
adds no call stack. Commit overlays pending writes, then advances PC, increments
the step, and consumes one unit of budget. A skipped instruction performs the
same step accounting without writes. Failure clears the computation and records
the reason.

```k
  rule <k> transfer(noFlow) => .K ... </k>
  rule <k> transfer(flow(_, T, C)) => eval(C) ~> flowGuard(T) ... </k>
  rule <k> 0 ~> flowGuard(_) => .K ... </k>
  rule <k> C:Int ~> flowGuard(T) => eval(T) ~> setTarget ... </k> requires C =/=Int 0
  rule <k> T:Int ~> setTarget => .K ... </k> <nextPc> _ => T </nextPc>
  rule <k> commit => finishStep ... </k>
       <pendingRegs> WR </pendingRegs> <pendingMemory> WM </pendingMemory>
       <regs> R => overlay(R, WR) </regs> <memory> M => overlay(M, WM) </memory>
  rule <k> skip => finishStep ... </k>
  rule <k> finishStep => next ... </k> <pc> _ => NEXT </pc> <nextPc> NEXT </nextPc>
       <step> N => N +Int 1 </step> <remaining> L => L -Int 1 </remaining>
  rule <k> fail(WHY) ~> _ => .K </k> <status> _ => WHY </status>
endmodule
```
