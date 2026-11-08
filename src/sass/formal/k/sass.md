# Instruction and program execution

`sass.md` defines `SASS`, importing exact floating point and bitvectors.
A program maps instruction addresses to lifted instructions. PC zero denotes
termination; each fall-through instruction advances PC by 16 bytes.

## IR syntax

An expression `node(ID, OP, W, ARGS)` carries a stable instruction-local id,
operator, result width, and argument list. `ordered.md` defines the instruction's
ordered statements. `unsupported` retains the reason and predicate of an
instruction whose semantics are unresolved.

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
  syntax Instruction ::= unsupported(Int, OptionalExpr, String)
  syntax Program ::= program(Map, Int)
  syntax Input ::= run(Program, Map, Map, Map, Map, Map, Int)
  syntax KItem ::= OptionalExpr | Instruction | Program
                 | location(String, Int) | byte(String, Int)
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
| `temporaries`, `orderedCode`, `orderedPosition`, `orderedLength` | Captured values and statement execution |
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
      <temporaries> .Map </temporaries>
      <orderedCode> .Map </orderedCode>
      <orderedPosition> 0 </orderedPosition>
      <orderedLength> 0 </orderedLength>
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

with zero for missing entries. Writes update these maps immediately.

```k
  syntax KItem ::= "next" | dispatch(Instruction) | gate(Instruction)
                 | eval(OptionalExpr) | evalArgs(List, List) | argument(List, List)
                 | remember(Int) | choose(Expr, Expr)
                 | compute(Int, String, Int, List)
                 | invokePure(Int, String, Int, List)
                 | "skip" | "finishStep" | fail(String)
  syntax Int ::= getInt(Map, KItem) [function]
               | elementBits(String) [function]
               | readRegs(String, Int, Int, Map) [function]
               | readBytes(String, Int, Int, Map) [function]
  syntax Map ::= writeRegs(String, Int, Int, Int, Map) [function]
               | writeBytes(String, Int, Int, Int, Map) [function]
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
  rule widths(.List) => .List
  rule widths(ListItem(A) REST) => ListItem(widthOf(A)) widths(REST)
  rule widthOf(node(_, _, W, _)) => W
  rule widthOf(_) => 0 [owise]
  rule isFloatOp(OP) =>
    OP ==String "fadd" orBool OP ==String "fmul" orBool OP ==String "ffma" orBool OP ==String "nanresult" orBool
    OP ==String "flt" orBool OP ==String "fle" orBool OP ==String "feq" orBool OP ==String "fnan" orBool OP ==String "fconv"
  rule isOrdinaryOp(OP) =>
    OP ==String "add" orBool OP ==String "sub" orBool OP ==String "mul" orBool OP ==String "and" orBool
    OP ==String "or" orBool OP ==String "xor" orBool OP ==String "not" orBool OP ==String "neg" orBool OP ==String "lzcount" orBool
    OP ==String "shl" orBool OP ==String "shr" orBool OP ==String "sar" orBool OP ==String "eq" orBool
    OP ==String "ne" orBool OP ==String "ult" orBool OP ==String "ule" orBool OP ==String "slt" orBool
    OP ==String "sle" orBool OP ==String "zext" orBool OP ==String "sext" orBool OP ==String "extract" orBool OP ==String "concat"

```

## Dispatch, predication, and termination

Dispatch resets memoized pure values. Ordered instruction dispatch also resets
temporaries and begins at statement zero. Predication is an explicit branch
within the body; an untaken branch evaluates none of its statements.

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
       <nextPc> _ => PC +Int 16 </nextPc>
    requires PC =/=Int 0 andBool N >Int 0 andBool PC in_keys(P)
  rule <k> dispatch(unsupported(A, G, WHY)) => eval(G) ~> gate(unsupported(A, G, WHY)) ... </k>
  rule <k> 0 ~> gate(_) => skip ... </k>
  rule <k> V:Int ~> gate(unsupported(_, _, WHY)) => fail(WHY) ... </k> requires V =/=Int 0

```

## Expression evaluation and sharing

The memoized-value rule has priority over evaluation. Each expression id is
evaluated at most once per instruction, including pure numerical contracts.
Conditional expressions evaluate the condition and only the chosen arm.
Other argument lists evaluate left to right. Missing parameters fail explicitly.

Register expressions are retained only for unsupported-instruction guards.
Supported instruction bodies use explicit reads. Integer and floating-point
operators dispatch to the preceding modules. Pure MUFU nodes request an
explicit response that leaves the environment unchanged.

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
  rule <k> ListItem(NAME:String) ARGS ~> compute(ID, "mufu", W, _)
           => invokePure(ID, "sass_prim_mufu_" +String NAME, W, ARGS) ~> remember(ID) ... </k>
  rule <k> VS:List ~> compute(ID, OP, W, WS) => bv(OP, W, VS, WS) ~> remember(ID) ... </k>
    requires isOrdinaryOp(OP)
  rule <k> VS:List ~> compute(ID, OP, W, WS) => fp(OP, W, VS, WS) ~> remember(ID) ... </k>
    requires isFloatOp(OP)

```

## Explicit runtime contracts

A runtime request is indexed by $(\text{step},\text{pc},\text{id})$. Its response
contains the name, evaluated arguments, result width, result value, events,
and environment before and after the operation.

Pure numerical response IDs are negative; ordered operation IDs are nonnegative
statement positions. This prevents collisions within an instruction. A numerical
approximation response must match its arguments and width, return
$0\leq v<2^w$, produce no events, and preserve the environment. `ordered.md`
checks typed operation result lists and applies their explicit environment changes.
Missing and mismatched responses fail.

This is an explicit assumption boundary. The rules do not give a default
answer to a primitive, model GPU scheduling, or establish that a supplied
response matches hardware. Proof-specific environment rules belong in a
separate contract module with documented assumptions.

```k
  rule <k> invokePure(ID, NAME, W, ARGS) => V ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> response(NAME, ARGS, W, V, .List, ENV, ENV)) ... </responses>
       <environment> ENV </environment>
    requires V >=Int 0 andBool V <Int (2 ^Int W)
  rule <k> invokePure(ID, _, _, _) => fail("runtime response missing") ... </k>
       <pc> PC </pc> <step> STEP </step> <responses> RESP </responses>
    requires notBool (request(STEP, PC, ID) in_keys(RESP))
  rule <k> invokePure(ID, NAME, W, ARGS) => fail("pure response mismatch") ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> response(N, A, WIDTH, V, EV, BEFORE, AFTER)) ... </responses>
       <environment> ENV </environment>
    requires NAME =/=String N orBool ARGS =/=K A orBool W =/=Int WIDTH orBool V <Int 0 orBool V >=Int (2 ^Int W)
             orBool EV =/=K .List orBool BEFORE =/=K ENV orBool AFTER =/=K ENV

```

## Instruction completion

Completion advances PC, increments the step, and consumes one unit of budget.
A skipped unsupported instruction performs the same accounting. Failure clears
the computation and records the reason; earlier statement writes remain visible.

```k
  rule <k> skip => finishStep ... </k>
  rule <k> finishStep => next ... </k> <pc> _ => NEXT </pc> <nextPc> NEXT </nextPc>
       <step> N => N +Int 1 </step> <remaining> L => L -Int 1 </remaining>
  rule <k> fail(WHY) ~> _ => .K </k> <status> _ => WHY </status>
endmodule
```
