requires "sass.k"

// Claims concern the executable IR. They import no runtime primitive axioms.
module SASS-CLAIMS
  imports SASS

  // A false guard preserves architectural state and advances by one instruction.
  claim <sass>
          <k> dispatch(instruction(1048576, node(0, "const", 1, ListItem(0)),
                                   .List, .List, .List, noFlow)) => next </k>
          <pc> 1048576 => 1048592 </pc>
          <nextPc> 1048592 </nextPc>
          <regs> R:Map </regs>
          <memory> M:Map </memory>
          <memo> .Map => (0 |-> 0) </memo>
          <step> N:Int => N +Int 1 </step>
          <remaining> L:Int => L -Int 1 </remaining>
          ...
        </sass>
    requires L >Int 0
    [label(predicated-off)]

  // Addition is modulo 2^W; the claim executes the IR operator rule.
  claim <k> eval(node(0, "add", 32,
                       ListItem(node(1, "param", 32, ListItem("x")))
                       ListItem(node(2, "param", 32, ListItem("y")))))
              => (X +Int Y) modInt 4294967296 ... </k>
        <params> ("x" |-> X:Int) ("y" |-> Y:Int) </params>
        <memo> .Map => (0 |-> ((X +Int Y) modInt 4294967296)) (1 |-> X) (2 |-> Y) </memo>
    requires X >=Int 0 andBool X <Int 4294967296 andBool Y >=Int 0 andBool Y <Int 4294967296
    [label(modular-add)]

  // Two writes exchange registers; both reads use the state before either write.
  claim <sass>
          <k> dispatch(instruction(1048576, absent, .List,
                ListItem(write("R", 0, 1, node(0, "reg", 32, ListItem("R") ListItem(1) ListItem(1))))
                ListItem(write("R", 1, 1, node(1, "reg", 32, ListItem("R") ListItem(0) ListItem(1)))),
                .List, noFlow)) => next </k>
          <pc> 1048576 => 1048592 </pc>
          <nextPc> 1048592 </nextPc>
          <regs> (location("R", 0) |-> X:Int) (location("R", 1) |-> Y:Int)
                  => (location("R", 0) |-> Y) (location("R", 1) |-> X) </regs>
          <memory> M:Map </memory>
          <memo> .Map => (0 |-> Y) (1 |-> X) </memo>
          <pendingRegs> .Map => (location("R", 0) |-> Y) (location("R", 1) |-> X) </pendingRegs>
          <pendingMemory> .Map </pendingMemory>
          <step> N:Int => N +Int 1 </step>
          <remaining> L:Int => L -Int 1 </remaining>
          ...
        </sass>
    requires L >Int 0 andBool X >=Int 0 andBool X <Int 4294967296 andBool Y >=Int 0 andBool Y <Int 4294967296
    [label(aliased-reads)]

  // A symbolic predicate chooses either edge of a short program; both reach EXIT.
  claim <sass>
          <k> next => .K </k>
          <program>
            (1048576 |-> instruction(1048576, absent, .List, .List, .List,
                flow("goto", node(0, "const", 64, ListItem(1048608)),
                     node(1, "param", 1, ListItem("p")))))
            (1048592 |-> instruction(1048592, absent, .List, .List, .List, noFlow))
            (1048608 |-> instruction(1048608, absent, .List, .List, .List,
                flow("return", node(0, "const", 64, ListItem(0)), absent)))
          </program>
          <pc> 1048576 => 0 </pc>
          <nextPc> 0 </nextPc>
          <params> ("p" |-> P:Int) </params>
          <regs> R:Map </regs>
          <memory> M:Map </memory>
          <memo> .Map => (0 |-> 0) </memo>
          <pendingRegs> .Map </pendingRegs>
          <pendingMemory> .Map </pendingMemory>
          <step> 0 => ?STEPS:Int </step>
          <remaining> 3 => ?LEFT:Int </remaining>
          <status> "running" => "halted" </status>
          ...
        </sass>
    requires P ==Int 0 orBool P ==Int 1
    [label(branching-program)]
endmodule
