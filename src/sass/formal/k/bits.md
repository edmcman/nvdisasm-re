# Integer bitvectors

`bits.md` defines `SASS-BITS`. Every value is an unsigned encoding of an
explicitly sized bitvector. These rules use K's unbounded integer arithmetic;
widths enter through normalization, masks, and signed interpretation.

## Vocabulary and imports

`bv(OP, W, VS, WS)` receives the operator name, result width, evaluated values,
and operand widths. A `ListItem` contributes one argument; `.List` is empty.

```k
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

```

## Width and signed interpretation

For a positive width $w$, normalization and signed interpretation are

$$
\operatorname{norm}_w(v)=v\bmod 2^w,\qquad
\operatorname{signed}_w(v)=
\begin{cases}
v & v<2^{w-1},\\
v-2^w & v\geq 2^{w-1}.
\end{cases}
$$

Signed interpretation assumes $0\leq v<2^w$. The exporter validates widths
and constant encodings; expression evaluation supplies normalized values.
`boolBit` turns a K Boolean into a one-bit encoding.

```k
  rule mask(W) => (2 ^Int W) -Int 1
  rule norm(V, W) => V modInt (2 ^Int W) requires W >Int 0
  rule signed(V, W) => V requires V <Int (2 ^Int (W -Int 1))
  rule signed(V, W) => V -Int (2 ^Int W) requires V >=Int (2 ^Int (W -Int 1))
  rule boolBit(true) => 1
  rule boolBit(false) => 0

  rule bv("add", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A +Int B, W)
```

## Arithmetic and Boolean operations

Addition, subtraction, multiplication, and negation wrap modulo $2^w$.
Boolean operations act directly on the unsigned encodings. Complement uses a
width-specific mask, so it changes only the low $w$ bits.

```k
  rule bv("sub", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A -Int B, W)
  rule bv("mul", W, ListItem(A:Int) ListItem(B:Int), _) => norm(A *Int B, W)
  rule bv("and", _, ListItem(A:Int) ListItem(B:Int), _) => A &Int B
  rule bv("or",  _, ListItem(A:Int) ListItem(B:Int), _) => A |Int B
  rule bv("xor", _, ListItem(A:Int) ListItem(B:Int), _) => A xorInt B
  rule bv("not", W, ListItem(A:Int), _) => A xorInt mask(W)
  rule bv("neg", W, ListItem(A:Int), _) => norm(0 -Int A, W)
  rule bv("shl", W, ListItem(A:Int) ListItem(N:Int), _) => norm(A <<Int N, W) requires N <Int W
```

## Shifts

A shift count at least as large as the operand width produces zero for left
and logical right shifts. Arithmetic right shift repeats the sign bit;
its oversized-count case shifts the signed value by $w-1$ and normalizes.

```k
  rule bv("shl", W, ListItem(_:Int) ListItem(N:Int), _) => 0 requires N >=Int W
  rule bv("shr", W, ListItem(A:Int) ListItem(N:Int), _) => A >>Int N requires N <Int W
  rule bv("shr", W, ListItem(_:Int) ListItem(N:Int), _) => 0 requires N >=Int W
  rule bv("sar", W, ListItem(A:Int) ListItem(N:Int), _) => norm(signed(A, W) >>Int N, W) requires N <Int W
  rule bv("sar", W, ListItem(A:Int) ListItem(N:Int), _) => norm(signed(A, W) >>Int (W -Int 1), W) requires N >=Int W
  rule bv("eq",  _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A ==Int B)
```

## Comparisons

Unsigned comparisons use the encodings directly. Signed comparisons use the
operand widths in `WS`, independently of the one-bit result width.

```k
  rule bv("ne",  _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A =/=Int B)
  rule bv("ult", _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A <Int B)
  rule bv("ule", _, ListItem(A:Int) ListItem(B:Int), _) => boolBit(A <=Int B)
  rule bv("slt", _, ListItem(A:Int) ListItem(B:Int), ListItem(X:Int) ListItem(Y:Int))
    => boolBit(signed(A, X) <Int signed(B, Y))
  rule bv("sle", _, ListItem(A:Int) ListItem(B:Int), ListItem(X:Int) ListItem(Y:Int))
    => boolBit(signed(A, X) <=Int signed(B, Y))
  rule bv("zext", _, ListItem(A:Int), _) => A
```

## Extensions, extraction, and concatenation

Zero extension keeps the encoding. Sign extension interprets the source width
before normalizing to the destination width. Extraction shifts by the low bit
index and masks to the result width. Concatenation places its first argument
in the most significant position.

```k
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
```
