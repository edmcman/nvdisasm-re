# Ordered statement execution

This module executes validated ordered bodies immediately. Atomic boundaries are
visible statements in this sequential model; there is no concurrent scheduler.
Runtime responses supply all operation results and explicit environment changes.
A suspending operation needs a completion response to proceed. This model does
not invent a result or replay earlier statements while that response is absent.

```k
requires "sass.md"

module SASS-ORDERED
  imports SASS

  syntax Instruction ::= orderedInstruction(Int, OptionalExpr, Map, Int)
  syntax KItem ::= orderedMove(Int, Expr)
                 | orderedRead(Int, String, Int, Int)
                 | orderedWrite(String, Int, Int, Expr)
                 | orderedLoad(Int, String, Expr, Int)
                 | orderedStore(String, Expr, Expr)
                 | orderedSpecial(Int, String, List, List, List)
                 | orderedBranch(Expr, Int, Int)
                 | orderedJump(Int)
                 | orderedBoundary(Int)
                 | orderedControl(String, Expr)
                 | "orderedExecute" | "orderedNext"
                 | orderedBind(Int) | orderedPutRegister(String, Int, Int)
                 | orderedLoadAddress(Int, String, Int)
                 | orderedStoreAddress(String, Expr) | orderedPutMemory(String, Int, Int)
                 | orderedCondition(Int, Int) | "orderedExit"
                 | orderedArguments(Int, String, List, List)
                 | orderedInvoke(Int, String, List, List)
                 | orderedBindResults(List)
                 | orderedResponse(String, List, List, List, List, Map, Map)
  syntax Bool ::= orderedValidResults(List, List) [function, total]

  rule <k> dispatch(orderedInstruction(A, G, CODE, N))
           => eval(G) ~> gate(orderedInstruction(A, G, CODE, N)) ... </k>
       <temporaries> _ => .Map </temporaries>
  rule <k> V:Int ~> gate(orderedInstruction(_, _, CODE, N)) => orderedExecute ... </k>
       <orderedCode> _ => CODE </orderedCode> <orderedPosition> _ => 0 </orderedPosition>
       <orderedLength> _ => N </orderedLength> requires V =/=Int 0
  rule <k> orderedExecute => finishStep ... </k>
       <orderedPosition> N </orderedPosition> <orderedLength> N </orderedLength>
  rule <k> orderedExecute => CODE[POSITION] ... </k>
       <orderedCode> CODE </orderedCode> <orderedPosition> POSITION </orderedPosition>
       <orderedLength> N </orderedLength> requires POSITION <Int N
  rule <k> orderedNext => orderedExecute ... </k> <orderedPosition> P => P +Int 1 </orderedPosition>

  rule <k> ListItem(T:Int) ~> compute(ID, "temp", _, _) => TEMPS[T] ~> remember(ID) ... </k>
       <temporaries> TEMPS </temporaries> requires T in_keys(TEMPS)
  rule <k> orderedMove(T, V) => eval(V) ~> orderedBind(T) ~> orderedNext ... </k>
  rule <k> V:Int ~> orderedBind(T) => .K ... </k> <temporaries> M => M[T <- V] </temporaries>
  rule <k> orderedRead(T, F, I, N) => readRegs(F, I, N, R) ~> orderedBind(T) ~> orderedNext ... </k>
       <regs> R </regs>
  rule <k> orderedWrite(F, I, N, V) => eval(V) ~> orderedPutRegister(F, I, N) ~> orderedNext ... </k>
  rule <k> V:Int ~> orderedPutRegister(F, I, N) => .K ... </k>
       <regs> R => writeRegs(F, I, N, V, R) </regs>
  rule <k> orderedLoad(T, S, A, W) => eval(A) ~> orderedLoadAddress(T, S, W) ~> orderedNext ... </k>
  rule <k> A:Int ~> orderedLoadAddress(T, S, W)
           => readBytes(S, A, W /Int 8, M) ~> orderedBind(T) ... </k> <memory> M </memory>
  rule <k> orderedStore(S, A, V) => eval(A) ~> orderedStoreAddress(S, V) ~> orderedNext ... </k>
  rule <k> A:Int ~> orderedStoreAddress(S, V) => eval(V) ~> orderedPutMemory(S, A, widthOf(V)) ... </k>
  rule <k> V:Int ~> orderedPutMemory(S, A, W) => .K ... </k>
       <memory> M => writeBytes(S, A, W /Int 8, V, M) </memory>

  rule <k> orderedBranch(C, A, B) => eval(C) ~> orderedCondition(A, B) ... </k>
  rule <k> 0 ~> orderedCondition(_, B) => orderedJump(B) ... </k>
  rule <k> V:Int ~> orderedCondition(A, _) => orderedJump(A) ... </k> requires V =/=Int 0
  rule <k> orderedJump(P) => orderedExecute ... </k> <orderedPosition> _ => P </orderedPosition>
  rule <k> orderedBoundary(_) => orderedNext ... </k>
  rule <k> orderedControl(_, T) => eval(T) ~> orderedExit ... </k>
  rule <k> T:Int ~> orderedExit => finishStep ... </k> <nextPc> _ => T </nextPc>

  rule <k> orderedSpecial(ID, NAME, ARGS, TARGETS, WIDTHS)
           => evalArgs(ARGS, .List) ~> orderedArguments(ID, NAME, TARGETS, WIDTHS) ... </k>
  rule <k> ARGS:List ~> orderedArguments(ID, NAME, TARGETS, WIDTHS)
           => orderedInvoke(ID, NAME, ARGS, WIDTHS) ~> orderedBindResults(TARGETS) ~> orderedNext ... </k>
  rule <k> orderedInvoke(ID, NAME, ARGS, WIDTHS) => VALUES ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> orderedResponse(NAME, ARGS, WIDTHS, VALUES, EV, BEFORE, AFTER)) ... </responses>
       <events> EVENTS => EVENTS EV </events> <environment> BEFORE => AFTER </environment>
    requires orderedValidResults(VALUES, WIDTHS)
  rule <k> orderedInvoke(ID, _, _, _) => fail("runtime response missing") ... </k>
       <pc> PC </pc> <step> STEP </step> <responses> RESP </responses>
    requires notBool (request(STEP, PC, ID) in_keys(RESP))
  rule <k> orderedInvoke(ID, NAME, ARGS, WIDTHS) => fail("runtime response mismatch") ... </k>
       <pc> PC </pc> <step> STEP </step>
       <responses> (request(STEP, PC, ID) |-> orderedResponse(N, A, W, VALUES, _, BEFORE, _)) ... </responses>
       <environment> ENV </environment>
    requires NAME =/=String N orBool ARGS =/=K A orBool WIDTHS =/=K W
             orBool notBool orderedValidResults(VALUES, WIDTHS) orBool BEFORE =/=K ENV
  rule orderedValidResults(.List, .List) => true
  rule orderedValidResults(ListItem(V:Int) VS, ListItem(W:Int) WS)
       => V >=Int 0 andBool V <Int (2 ^Int W) andBool orderedValidResults(VS, WS)
  rule orderedValidResults(_, _) => false [owise]
  rule <k> .List ~> orderedBindResults(.List) => .K ... </k>
  rule <k> ListItem(V:Int) VS ~> orderedBindResults(ListItem(T:Int) TS)
           => VS ~> orderedBindResults(TS) ... </k> <temporaries> M => M[T <- V] </temporaries>
endmodule
```
