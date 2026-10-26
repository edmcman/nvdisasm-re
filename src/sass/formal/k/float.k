requires "bits.k"

// Exact binary arithmetic uses dyadics S * 2^E, never host floating point.
module SASS-FLOAT
  imports SASS-BITS

  syntax FP ::= fpZero(Int) | fpFinite(Int, Int) | fpInf(Int) | "fpNaN"
  syntax Int ::= fractionBits(Int) [function]
               | exponentBits(Int) [function]
               | bias(Int) [function]
               | fpSign(Int, Int) [function]
               | nanBits(Int) [function]
               | infBits(Int, Int) [function]
               | finiteBits(Int, Int, Int, String, Int) [function]
               | packBits(Int, Int, Int, String, Int) [function]
               | packRounded(Int, Int, Int, String, Int) [function]
               | fpOverflow(Int, Int, String) [function]
               | log2Floor(Int) [function]
               | dyadicRound(Int, Int, String) [function]
               | roundedMagnitude(Int, Int, String, Bool) [function]
               | roundBump(Int, Int, Int, String, Bool) [function]
               | fpEncode(FP, Int, String, Int) [function]
               | fp(String, Int, List, List) [function]
               | fpCompare(String, FP, FP) [function]
               | compareFinite(String, Int, Int, Int, Int) [function]
               | compareIntegers(String, Int, Int) [function]
               | fpAddZeroSign(FP, FP, String) [function]
               | fpFmaZeroSign(FP, FP, FP, String) [function]
  syntax FP ::= decodeFP(Int, Int) [function]
              | decodeFields(Int, Int, Int, Int) [function]
              | fpAdd(FP, FP) [function]
              | fpMul(FP, FP) [function]
              | scaleFP(FP, Int) [function]
              | fpFma(FP, FP, FP) [function]
  syntax Bool ::= fpIsNaN(FP) [function, total]
                 | fpNegative(FP) [function]

  rule fractionBits(16) => 10
  rule fractionBits(32) => 23
  rule fractionBits(64) => 52
  rule exponentBits(16) => 5
  rule exponentBits(32) => 8
  rule exponentBits(64) => 11
  rule bias(W) => (2 ^Int (exponentBits(W) -Int 1)) -Int 1
  rule fpSign(B, W) => B >>Int (W -Int 1)
  rule nanBits(W) => (mask(exponentBits(W)) <<Int fractionBits(W)) |Int (1 <<Int (fractionBits(W) -Int 1))
  rule infBits(S, W) => (S <<Int (W -Int 1)) |Int (mask(exponentBits(W)) <<Int fractionBits(W))
  rule decodeFP(B, W) => decodeFields(fpSign(B, W), (B >>Int fractionBits(W)) &Int mask(exponentBits(W)),
                                    B &Int mask(fractionBits(W)), W)
  rule decodeFields(S, E, 0, W) => fpInf(S) requires E ==Int mask(exponentBits(W))
  rule decodeFields(_, E, M, W) => fpNaN requires E ==Int mask(exponentBits(W)) andBool M =/=Int 0
  rule decodeFields(S, 0, 0, _) => fpZero(S)
  rule decodeFields(S, 0, M, W) => fpFinite((1 -Int (2 *Int S)) *Int M, 1 -Int bias(W) -Int fractionBits(W))
    requires M >Int 0
  rule decodeFields(S, E, M, W) => fpFinite((1 -Int (2 *Int S)) *Int ((1 <<Int fractionBits(W)) +Int M),
                                          E -Int bias(W) -Int fractionBits(W))
    requires E >Int 0 andBool E <Int mask(exponentBits(W))

  rule log2Floor(X) => 0 requires X >=Int 1 andBool X <Int 2
  rule log2Floor(X) => 1 +Int log2Floor(X >>Int 1) requires X >=Int 2

  // Rounding is performed once, including a fused multiply-add and scaled multiply.
  rule dyadicRound(S, D, _) => S <<Int D requires D >=Int 0
  rule dyadicRound(S, D, R) => (1 -Int (2 *Int boolBit(S <Int 0))) *Int
                              roundedMagnitude(absInt(S), 2 ^Int (0 -Int D), R, S <Int 0)
    requires D <Int 0
  rule roundedMagnitude(A, D, R, NEG) => (A /Int D) +Int roundBump(A /Int D, A modInt D, D, R, NEG)
  rule roundBump(_, 0, _, _, _) => 0
  rule roundBump(_, REM, _, "RZ", _) => 0 requires REM >Int 0
  rule roundBump(_, REM, _, "RM", NEG) => boolBit(NEG) requires REM >Int 0
  rule roundBump(_, REM, _, "RP", NEG) => boolBit(notBool NEG) requires REM >Int 0
  rule roundBump(Q, REM, D, "RN", _) => boolBit((2 *Int REM >Int D) orBool
                                                           ((2 *Int REM ==Int D) andBool (Q modInt 2 ==Int 1)))
    requires REM >Int 0

  rule packBits(0, _, W, _, Z) => Z <<Int (W -Int 1)
  rule packBits(S, E, W, R, Z) => finiteBits(S, E, W, R, Z) requires S =/=Int 0
  rule finiteBits(S, E, W, R, Z)
    => packRounded(dyadicRound(S, E -Int (maxInt(log2Floor(absInt(S)) +Int E, 1 -Int bias(W)) -Int fractionBits(W)), R),
                   log2Floor(absInt(S)) +Int E, W, R, Z)
  rule packRounded(S, E, W, R, Z) => packRounded(S >>Int 1, E +Int 1, W, R, Z)
    requires absInt(S) >=Int (1 <<Int (fractionBits(W) +Int 1))
  rule packRounded(S, E, W, R, _) => fpOverflow(boolBit(S <Int 0), W, R)
    requires absInt(S) <Int (1 <<Int (fractionBits(W) +Int 1)) andBool E >Int bias(W)
  rule packRounded(S, E, W, _, _) => (boolBit(S <Int 0) <<Int (W -Int 1)) |Int
                                    ((maxInt(E, 1 -Int bias(W)) +Int bias(W)) <<Int fractionBits(W)) |Int
                                    (absInt(S) -Int (1 <<Int fractionBits(W)))
    requires E <=Int bias(W) andBool absInt(S) >=Int (1 <<Int fractionBits(W)) andBool
             absInt(S) <Int (1 <<Int (fractionBits(W) +Int 1))
  // The sign comes from the unrounded significand even if rounding produces zero.
  rule packRounded(S, E, W, _, Z) => (Z <<Int (W -Int 1)) |Int absInt(S)
    requires E <=Int bias(W) andBool absInt(S) <Int (1 <<Int fractionBits(W))
  rule fpOverflow(S, W, R) => infBits(S, W)
    requires R ==String "RN" orBool (R ==String "RP" andBool S ==Int 0) orBool (R ==String "RM" andBool S ==Int 1)
  rule fpOverflow(S, W, R) => (S <<Int (W -Int 1)) |Int ((mask(exponentBits(W)) -Int 1) <<Int fractionBits(W)) |Int mask(fractionBits(W))
    requires R ==String "RZ" orBool (R ==String "RP" andBool S ==Int 1) orBool (R ==String "RM" andBool S ==Int 0)
  rule fpEncode(fpNaN, W, _, _) => nanBits(W)
  rule fpEncode(fpInf(S), W, _, _) => infBits(S, W)
  rule fpEncode(fpZero(S), W, _, _) => S <<Int (W -Int 1)
  rule fpEncode(fpFinite(S, E), W, R, Z) => packBits(S, E, W, R, boolBit(S <Int 0)) requires S =/=Int 0
  rule fpEncode(fpFinite(0, E), W, R, Z) => packBits(0, E, W, R, Z)

  rule fpIsNaN(fpNaN) => true
  rule fpIsNaN(_) => false [owise]
  rule fpNegative(fpZero(S)) => S ==Int 1
  rule fpNegative(fpInf(S)) => S ==Int 1
  rule fpNegative(fpFinite(S, _)) => S <Int 0
  rule fpNegative(fpNaN) => false

  rule fpAdd(fpNaN, _) => fpNaN
  rule fpAdd(A, fpNaN) => fpNaN requires notBool fpIsNaN(A)
  rule fpAdd(fpInf(S), fpInf(T)) => fpNaN requires S =/=Int T
  rule fpAdd(fpInf(S), fpInf(S)) => fpInf(S)
  rule fpAdd(fpInf(S), fpZero(_)) => fpInf(S)
  rule fpAdd(fpInf(S), fpFinite(_, _)) => fpInf(S)
  rule fpAdd(fpZero(_), fpInf(S)) => fpInf(S)
  rule fpAdd(fpFinite(_, _), fpInf(S)) => fpInf(S)
  rule fpAdd(fpZero(_), fpZero(_)) => fpFinite(0, 0)
  rule fpAdd(fpZero(_), fpFinite(S, E)) => fpFinite(S, E)
  rule fpAdd(fpFinite(S, E), fpZero(_)) => fpFinite(S, E)
  rule fpAdd(fpFinite(A, E), fpFinite(B, F))
    => fpFinite((A <<Int (E -Int minInt(E, F))) +Int (B <<Int (F -Int minInt(E, F))), minInt(E, F))
  rule fpAddZeroSign(fpZero(S), fpZero(S), _) => S
  rule fpAddZeroSign(_, _, R) => boolBit(R ==String "RM") [owise]

  rule fpMul(fpNaN, _) => fpNaN
  rule fpMul(A, fpNaN) => fpNaN requires notBool fpIsNaN(A)
  rule fpMul(fpInf(_), fpZero(_)) => fpNaN
  rule fpMul(fpZero(_), fpInf(_)) => fpNaN
  rule fpMul(fpInf(S), fpInf(T)) => fpInf(S xorInt T)
  rule fpMul(fpInf(S), fpFinite(B, _)) => fpInf(S xorInt boolBit(B <Int 0))
  rule fpMul(fpFinite(A, _), fpInf(T)) => fpInf(boolBit(A <Int 0) xorInt T)
  rule fpMul(fpZero(S), fpZero(T)) => fpZero(S xorInt T)
  rule fpMul(fpZero(S), fpFinite(B, _)) => fpZero(S xorInt boolBit(B <Int 0))
  rule fpMul(fpFinite(A, _), fpZero(T)) => fpZero(boolBit(A <Int 0) xorInt T)
  rule fpMul(fpFinite(A, E), fpFinite(B, F)) => fpFinite(A *Int B, E +Int F)
  rule scaleFP(fpFinite(S, E), K) => fpFinite(S, E +Int K)
  rule scaleFP(A, _) => A [owise]
  rule fpFma(A, B, C) => fpAdd(fpMul(A, B), C)
  rule fpFmaZeroSign(A, B, fpZero(T), _) => T
    requires notBool fpIsNaN(A) andBool notBool fpIsNaN(B) andBool
             (isZero(A) orBool isZero(B)) andBool ((fpNegative(A) xorBool fpNegative(B)) ==Bool (T ==Int 1))
  rule fpFmaZeroSign(_, _, _, R) => boolBit(R ==String "RM") [owise]
  syntax Bool ::= isZero(FP) [function, total]
  rule isZero(fpZero(_)) => true
  rule isZero(_) => false [owise]

  rule fp("fadd", W, ListItem(A:Int) ListItem(B:Int) ListItem(R:String), _)
    => fpEncode(fpAdd(decodeFP(A, W), decodeFP(B, W)), W, R, fpAddZeroSign(decodeFP(A, W), decodeFP(B, W), R))
  rule fp("fmul", W, ListItem(A:Int) ListItem(B:Int) ListItem(R:String) ListItem(K:Int), _)
    => fpEncode(scaleFP(fpMul(decodeFP(A, W), decodeFP(B, W)), K), W, R, 0)
  rule fp("ffma", W, ListItem(A:Int) ListItem(B:Int) ListItem(C:Int) ListItem(R:String), _)
    => fpEncode(fpFma(decodeFP(A, W), decodeFP(B, W), decodeFP(C, W)), W, R,
                fpFmaZeroSign(decodeFP(A, W), decodeFP(B, W), decodeFP(C, W), R))
  rule fp("nanresult", W, ListItem(A:Int) ListItem(B:Int), _) => B requires fpIsNaN(decodeFP(A, W))
  rule fp("nanresult", W, ListItem(A:Int) ListItem(_:Int), _) => A requires notBool fpIsNaN(decodeFP(A, W))
  rule fp("fnan", _, ListItem(A:Int), ListItem(W:Int)) => boolBit(fpIsNaN(decodeFP(A, W)))
  rule fp("fconv", W, ListItem(A:Int) ListItem(R:String), ListItem(X:Int) ListItem(_:Int))
    => convertFP(A, X, W, R)
  syntax Int ::= convertFP(Int, Int, Int, String) [function]
  rule convertFP(A, X, W, _) => (fpSign(A, X) <<Int (W -Int 1)) |Int nanBits(W)
    requires fpIsNaN(decodeFP(A, X))
  rule convertFP(A, X, W, R) => fpEncode(decodeFP(A, X), W, R, 0)
    requires notBool fpIsNaN(decodeFP(A, X))
  rule fp(OP, _, ListItem(A:Int) ListItem(B:Int), ListItem(W:Int) ListItem(_:Int))
    => fpCompare(OP, decodeFP(A, W), decodeFP(B, W))
    requires OP ==String "flt" orBool OP ==String "fle" orBool OP ==String "feq"

  // Order infinities explicitly; all comparisons involving a NaN are false.
  rule fpCompare(_, fpNaN, _) => 0
  rule fpCompare(_, A, fpNaN) => 0 requires notBool fpIsNaN(A)
  rule fpCompare(OP, fpInf(S), fpInf(T)) => compareIntegers(OP, 1 -Int S, 1 -Int T)
  rule fpCompare(OP, fpInf(S), fpZero(_)) => compareIntegers(OP, 1 -Int (2 *Int S), 0)
  rule fpCompare(OP, fpInf(S), fpFinite(_, _)) => compareIntegers(OP, 1 -Int (2 *Int S), 0)
  rule fpCompare(OP, fpZero(_), fpInf(S)) => compareIntegers(OP, 0, 1 -Int (2 *Int S))
  rule fpCompare(OP, fpFinite(_, _), fpInf(S)) => compareIntegers(OP, 0, 1 -Int (2 *Int S))
  rule fpCompare(OP, fpZero(_), fpZero(_)) => compareIntegers(OP, 0, 0)
  rule fpCompare(OP, fpZero(_), fpFinite(B, F)) => compareFinite(OP, 0, F, B, F)
  rule fpCompare(OP, fpFinite(A, E), fpZero(_)) => compareFinite(OP, A, E, 0, E)
  rule fpCompare(OP, fpFinite(A, E), fpFinite(B, F)) => compareFinite(OP, A, E, B, F)
  rule compareFinite(OP, A, E, B, F)
    => compareIntegers(OP, A <<Int (E -Int minInt(E, F)), B <<Int (F -Int minInt(E, F)))
  rule compareIntegers("flt", A, B) => boolBit(A <Int B)
  rule compareIntegers("fle", A, B) => boolBit(A <=Int B)
  rule compareIntegers("feq", A, B) => boolBit(A ==Int B)
endmodule
