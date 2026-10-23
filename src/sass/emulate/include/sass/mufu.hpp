// MUFU.SIN/COS: the fixed-point model recovered from SM89 GPU outputs (docs/hardware-facts.md,
// research/mufu/sin-fit/QUANTIZATION.md); every one of the 2^23 reduced grid offsets matches hardware.
#pragma once
#include <bit>
#include <cstdint>

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

// The positive result word at an integer first-quadrant offset in [0, 2^23).
inline uint32_t reduced_sine(uint32_t offset) {
  const int64_t segment = offset >> 17, local = offset & 0x1ffff, high = local >> 10, low = local & 1023;
  int64_t square = 2 * high * high;
  for (int bit = 0; bit < 7; ++bit) square += ((high >> bit) & 1) * (low >> (8 - bit));
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
}  // namespace sass::mufu
