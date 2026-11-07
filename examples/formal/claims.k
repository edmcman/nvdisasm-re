requires "ordered.md"

// Claims concern the executable IR. They import no runtime primitive axioms.
module SASS-CLAIMS
  imports SASS-ORDERED

  // An untaken arm does not evaluate its missing parameter.
  claim <sass>
          <k> dispatch(orderedInstruction(1048576, absent,
                (0 |-> orderedBranch(node(-1, "const", 1, ListItem(0)), 1, 2))
                (1 |-> orderedWrite("R", 0, 1, node(-2, "param", 32, ListItem("missing")))),
                2)) => next </k>
          <pc> 1048576 => 1048592 </pc>
          <nextPc> 1048592 </nextPc>
          <regs> R:Map </regs>
          <memory> M:Map </memory>
          <memo> .Map => (-1 |-> 0) </memo>
          <temporaries> .Map </temporaries>
          <orderedCode> _CODE:Map => ?_CODE:Map </orderedCode>
          <orderedPosition> _POS:Int => 2 </orderedPosition>
          <orderedLength> _LEN:Int => 2 </orderedLength>
          <step> N:Int => N +Int 1 </step>
          <remaining> L:Int => L -Int 1 </remaining>
          ...
        </sass>
    requires L >Int 0
    [label(predicated-off)]

  // Addition is modulo 2^W; the claim executes the IR operator rule.
  claim <k> eval(node(-1, "add", 32,
                       ListItem(node(-2, "param", 32, ListItem("x")))
                       ListItem(node(-3, "param", 32, ListItem("y")))))
              => (X +Int Y) modInt 4294967296 ... </k>
        <params> ("x" |-> X:Int) ("y" |-> Y:Int) </params>
        <memo> .Map => (-1 |-> ((X +Int Y) modInt 4294967296)) (-2 |-> X) (-3 |-> Y) </memo>
    requires X >=Int 0 andBool X <Int 4294967296 andBool Y >=Int 0 andBool Y <Int 4294967296
    [label(modular-add)]

  // Explicit reads capture both inputs before either write.
  claim <sass>
          <k> dispatch(orderedInstruction(1048576, absent,
                (0 |-> orderedRead(0, "R", 1, 1))
                (1 |-> orderedRead(1, "R", 0, 1))
                (2 |-> orderedWrite("R", 0, 1, node(-1, "temp", 32, ListItem(0))))
                (3 |-> orderedWrite("R", 1, 1, node(-2, "temp", 32, ListItem(1)))),
                4)) => next </k>
          <pc> 1048576 => 1048592 </pc>
          <nextPc> 1048592 </nextPc>
          <regs> (location("R", 0) |-> X:Int) (location("R", 1) |-> Y:Int)
                  => (location("R", 0) |-> Y) (location("R", 1) |-> X) </regs>
          <memory> M:Map </memory>
          <memo> .Map => (-1 |-> Y) (-2 |-> X) </memo>
          <temporaries> _TEMPS:Map => (0 |-> Y) (1 |-> X) </temporaries>
          <orderedCode> _CODE:Map => ?_CODE:Map </orderedCode>
          <orderedPosition> _POS:Int => 4 </orderedPosition>
          <orderedLength> _LEN:Int => 4 </orderedLength>
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
            (1048576 |-> orderedInstruction(1048576, absent,
                (0 |-> orderedBranch(node(-2, "param", 1, ListItem("p")), 1, 2))
                (1 |-> orderedControl("goto", node(-1, "const", 64, ListItem(1048608)))),
                2))
            (1048592 |-> orderedInstruction(1048592, absent, .Map, 0))
            (1048608 |-> orderedInstruction(1048608, absent,
                (0 |-> orderedControl("return", node(-1, "const", 64, ListItem(0)))), 1))
          </program>
          <pc> 1048576 => 0 </pc>
          <nextPc> 0 </nextPc>
          <params> ("p" |-> P:Int) </params>
          <regs> R:Map </regs>
          <memory> M:Map </memory>
          <memo> .Map => (-1 |-> 0) </memo>
          <temporaries> .Map </temporaries>
          <orderedCode> _CODE:Map => ?_CODE:Map </orderedCode>
          <orderedPosition> _POS:Int => 0 </orderedPosition>
          <orderedLength> _LEN:Int => 1 </orderedLength>
          <step> 0 => ?STEPS:Int </step>
          <remaining> 3 => ?LEFT:Int </remaining>
          <status> "running" => "halted" </status>
          ...
        </sass>
    requires P ==Int 0 orBool P ==Int 1
    [label(branching-program)]
endmodule
