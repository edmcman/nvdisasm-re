// Executable, width-explicit bitvector semantics. Values are unsigned encodings.
module SASS-BITS
  imports INT
  imports BOOL
  imports STRING
  imports LIST

  syntax Int ::= mask(Int) [function, total]
               | norm(Int, Int) [function]
               | signed(Int, Int) [function]
               | boolBit(Bool) [function, total]
               | bv(String, Int, List, List) [function]
               | catBits(List, List) [function]

  rule mask(W) => (2 ^Int W) -Int 1
  rule norm(V, W) => V modInt (2 ^Int W) requires W >Int 0
  rule signed(V, W) => V requires V <Int (2 ^Int (W -Int 1))
  rule signed(V, W) => V -Int (2 ^Int W) requires V >=Int (2 ^Int (W -Int 1))
  rule boolBit(true) => 1
  rule boolBit(false) => 0

  rule bv("add", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A +Int B, W)
  rule bv("sub", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A -Int B, W)
  rule bv("mul", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A *Int B, W)
  rule bv("and", _, ListItem(A:Int) ListItem(B:Int), _) => A &Int B
  rule bv("or",  _, ListItem(A:Int) ListItem(B:Int), _) => A |Int B
  rule bv("xor", _, ListItem(A:Int) ListItem(B:Int), _) => A xorInt B
  rule bv("not", W, ListItem(A:Int), _) => A xorInt mask(W)
  rule bv("neg", W, ListItem(A:Int), _) => norm(0 -Int A, W)
  rule bv("shl", W, ListItem(A:Int) ListItem(N:Int), _) => norm(A <<Int N, W) requires N <Int W
  rule bv("shl", W, ListItem(_:Int) ListItem(N:Int), _) => 0 requires N >=Int W
  rule bv("shr", W, ListItem(A:Int) ListItem(N:Int), _) => A >>Int N requires N <Int W
  rule bv("shr", W, ListItem(_:Int) ListItem(N:Int), _) => 0 requires N >=Int W
  rule bv("sar", W, ListItem(A:Int) ListItem(N:Int), _) => norm(signed(A, W) >>Int N, W) requires N <Int W
  rule bv("sar", W, ListItem(A:Int) ListItem(N:Int), _) => norm(signed(A, W) >>Int (W -Int 1), W) requires N >=Int W
  rule bv("eq",  _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A ==Int B)
  rule bv("ne",  _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A =/=Int B)
  rule bv("ult", _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A <Int B)
  rule bv("ule", _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A <=Int B)
  rule bv("slt", _, ListItem(A:Int) ListItem(B:Int), ListItem(X:Int) ListItem(Y:Int))
    => boolBit(signed(A, X) <Int signed(B, Y))
  rule bv("sle", _, ListItem(A:Int) ListItem(B:Int), ListItem(X:Int) ListItem(Y:Int))
    => boolBit(signed(A, X) <=Int signed(B, Y))
  rule bv("zext", _, ListItem(A:Int), _) => A
  rule bv("sext", W, ListItem(A:Int), ListItem(X:Int)) => norm(signed(A, X), W)
  rule bv("extract", W, ListItem(A:Int) ListItem(_:Int) ListItem(L:Int), _) => (A >>Int L) &Int mask(W)
  rule bv("concat", _, VS, WS) => catBits(VS, WS)
  rule catBits(.List, .List) => 0
  rule catBits(ListItem(A:Int) REST, ListItem(W:Int) WIDTHS)
    => (A <<Int sumWidths(WIDTHS)) |Int catBits(REST, WIDTHS)
  syntax Int ::= sumWidths(List) [function]
  rule sumWidths(.List) => 0
  rule sumWidths(ListItem(W:Int) REST) => W +Int sumWidths(REST)
endmodule
