// SM89 MUFU fixed-point models: docs/hardware-facts.md and research/mufu/.
#pragma once
#include <bit>
#include <cstdint>
#include "sass/reciprocal_coefficients.hpp"
#include "sass/softfloat.hpp"

namespace sass::mufu {
// Hardware-recovered coefficients: constant / 2^25, linear / 2^20, quadratic / 2^22.
inline constexpr int64_t COEFFICIENTS[64][3] = {
    {0, 25737, -14},
    {823463, 25730, -48},
    {1646436, 25706, -76},
    {2468414, 25668, -110},
    {3288906, 25613, -137},
    {4107418, 25544, -171},
    {4923453, 25459, -201},
    {5736526, 25358, -229},
    {6546141, 25243, -262},
    {7351816, 25112, -292},
    {8153060, 24966, -322},
    {8949394, 24805, -352},
    {9740335, 24629, -381},
    {10525413, 24438, -410},
    {11304145, 24233, -441},
    {12076070, 24013, -470},
    {12840725, 23778, -497},
    {13597643, 23529, -525},
    {14346367, 23266, -553},
    {15086452, 22989, -581},
    {15817450, 22698, -608},
    {16538916, 22394, -637},
    {17250422, 22076, -664},
    {17951542, 21744, -688},
    {18641840, 21400, -715},
    {19320916, 21042, -738},
    {19988350, 20672, -763},
    {20643746, 20290, -790},
    {21286703, 19895, -812},
    {21916841, 19489, -839},
    {22533776, 19070, -859},
    {23137138, 18640, -881},
    {23726565, 18199, -904},
    {24301698, 17747, -926},
    {24862194, 17284, -946},
    {25407711, 16811, -967},
    {25937924, 16328, -988},
    {26452518, 15834, -1004},
    {26951170, 15332, -1025},
    {27433593, 14820, -1043},
    {27899488, 14299, -1059},
    {28348583, 13769, -1074},
    {28780594, 13232, -1093},
    {29195273, 12686, -1107},
    {29592364, 12133, -1123},
    {29971632, 11572, -1136},
    {30332846, 11004, -1148},
    {30675788, 10430, -1162},
    {31000253, 9849, -1172},
    {31306042, 9263, -1185},
    {31592974, 8671, -1196},
    {31860879, 8073, -1203},
    {32109590, 7471, -1213},
    {32338961, 6864, -1220},
    {32548845, 6254, -1230},
    {32739132, 5639, -1236},
    {32909694, 5021, -1242},
    {33060432, 4400, -1247},
    {33191257, 3776, -1250},
    {33302087, 3151, -1258},
    {33392857, 2523, -1260},
    {33463517, 1893, -1260},
    {33514014, 1263, -1263},
    {33544326, 632, -1265}};

inline int64_t retained_square(uint32_t local, int width) {
  const int low_bits = width - 7;
  const int64_t high = local >> low_bits, low = local & ((1 << low_bits) - 1);
  int64_t square = 2 * high * high;
  for (int bit = 0; bit < 7; ++bit) {
    const int shift = low_bits - 2 - bit;
    square += ((high >> bit) & 1) * (shift >= 0 ? low >> shift : low << -shift);
  }
  return square;
}

// The positive result word at an integer first-quadrant offset in [0, 2^23).
inline uint32_t reduced_sine(uint32_t offset) {
  const int64_t segment = offset >> 17, local = offset & 0x1ffff, square = retained_square(local, 17);
  const auto [a, b, c] = COEFFICIENTS[segment];
  const int64_t accumulator = (a << 12) + b * local + c * square;
  if (!accumulator) return 0;
  const int leading = 63 - std::countl_zero(uint64_t(accumulator));
  const uint64_t significand = leading >= 23 ? uint64_t(accumulator) >> (leading - 23) : uint64_t(accumulator) << (23 - leading);
  return uint32_t(leading - 37 + 127) << 23 | uint32_t(significand & 0x7fffff);
}

// F32 SIN/COS bits; source modifiers are applied by the caller. The argument is in turns.
inline uint32_t sincos(uint32_t bits, bool cosine) {
  const uint32_t exponent = bits >> 23 & 255;
  if (exponent == 255) return 0x7fffffff;
  // significand * 2^(exponent-150), scaled by 2^25 and truncated.
  uint32_t phase = 0;
  if (exponent != 0 && exponent >= 102 && exponent < 150) {
    const uint64_t significand = 1u << 23 | (bits & 0x7fffff);
    const int shift = int(exponent) - 125;
    phase = uint32_t((shift >= 0 ? significand << shift : significand >> -shift) & 0x1ffffff);
  }
  if (cosine) phase = (phase + (1u << 23)) & 0x1ffffff;
  const uint32_t quadrant = phase >> 23;
  uint32_t offset = phase & 0x7fffff;
  if (quadrant & 1) offset ^= 0x7fffff;
  const bool negative = (quadrant >= 2) ^ (!cosine && (bits >> 31));
  return reduced_sine(offset) | uint32_t(negative) << 31;
}

inline softfloat::u128 square_root(softfloat::u128 value) {
  softfloat::u128 result = 0, bit = softfloat::u128(1) << 126;
  while (bit > value) bit >>= 2;
  while (bit) {
    if (value >= result + bit) value -= result + bit, result = (result >> 1) + bit;
    else result >>= 1;
    bit >>= 2;
  }
  return result;
}

template <int W> inline uint64_t reciprocal(uint64_t bits, bool rsq) {
  using namespace softfloat;
  const auto x = unpack<W>(bits);
  const uint64_t sign = uint64_t(x.term.sign) << (W - 1);
  const uint64_t canonical = (uint64_t(1) << (W - 1)) - 1;
  if (x.kind == NaN) return canonical;
  const bool subnormal = W != 16 && !(bits & (exponents<W>() << Format<W>::M));
  if (x.kind == Zero || subnormal) return inf<W>(x.term.sign);
  if (rsq && x.term.sign) return canonical;
  if (x.kind == Inf) return sign;
  u128 significand;
  int exponent;
  if constexpr (W != 16) {
    const int fraction_bits = W == 32 ? 23 : 20;
    const uint32_t fraction = uint32_t(W == 32 ? bits : bits >> 32) & ((1 << fraction_bits) - 1);
    const int input_exponent = int(bits >> Format<W>::M & exponents<W>()) - bias<W>();
    const int parity = rsq ? input_exponent & 1 : 0;
    const int width = fraction_bits - (rsq ? 6 : 7), segment = fraction >> width;
    const int64_t local = fraction & ((1 << width) - 1);
    const int64_t (*coefficients)[3] = W == 32 ? (rsq ? RSQ32[parity] : RCP32) : (rsq ? RSQ64[parity] : RCP64);
    const auto [a, b, c] = coefficients[segment];
    significand = !fraction && !parity ? int64_t(1) << 57 :
                  (a << 15) + b * local + c * retained_square(uint32_t(local), width) * 4;
    exponent = -57 - (rsq ? (input_exponent - parity) / 2 : input_exponent);
  } else {
    auto t = x.term;
    const int zeros = std::countr_zero(uint64_t(t.sig));
    t.sig >>= zeros, t.exp += zeros;
    if (rsq) {
      if (t.exp % 2) t.sig <<= 1, --t.exp;
      significand = square_root((u128(1) << 120) / t.sig);
      exponent = -60 - t.exp / 2;
    } else {
      significand = (u128(1) << 120) / t.sig;
      exponent = -120 - t.exp;
    }
  }
  if constexpr (W == 16)
    if (msb(significand) + exponent > bias<W>()) return inf<W>(x.term.sign);
  uint64_t result = pack<W>({rsq ? 0 : x.term.sign, significand, exponent}, W == 32 ? RN : RZ, 0);
  if constexpr (W == 16)
    if (rsq && (bits & 0x7ff) == 0x713) ++result;
  if constexpr (W != 16)
    if (!(result & (exponents<W>() << Format<W>::M))) result &= uint64_t(1) << (W - 1);
  return result;
}
}  // namespace sass::mufu
