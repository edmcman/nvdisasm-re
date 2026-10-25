// C entry points to the primitives for Python (sass.emulate.native): the runtime's primitive calls
// by name, and test hooks for the floating point and MUFU models.
#include <cctype>
#include <cstring>
#include <string_view>
#include "sass/primitives.hpp"
#include "sass/softfloat.hpp"

using namespace sass;

namespace {
constexpr std::string_view REDUCTIONS[] = {"and", "or", "xor", "add", "umin", "umax", "smin", "smax"};

prim::Status call(std::string_view name, const prim::Warp &w, uint32_t need, const uint64_t *args, int nargs, uint64_t *out) {
  const auto arg = [&](int i) { return args + 32 * i; };
  if (name == "read_lane" && nargs == 2) return prim::read_lane(w, need, arg(0), arg(1), out);
  if (name == "ballot" && nargs == 1) return prim::ballot(w, need, arg(0), out);
  if (name == "match" && nargs == 1) return prim::match(w, need, arg(0), out);
  if (name == "address_space" && nargs == 1) return prim::address_space(w, need, arg(0), out);
  if (name.starts_with("redux_") && nargs == 1)
    for (int op = 0; op < 8; ++op)
      if (name.substr(6) == REDUCTIONS[op]) return prim::redux(prim::Reduction(op), w, need, arg(0), out);
  if (name.starts_with("mufu_") && (nargs == 1 || nargs == 2))
    for (int fn = 0; fn < 10; ++fn) {
      std::string upper(name.substr(5));
      for (auto &c : upper) c = char(std::toupper(c));
      if (upper == prim::MUFU_NAMES[fn]) return prim::mufu(prim::Mufu(fn), nargs == 2, need, arg(0), out);
    }
  return {nullptr, "primitive " + std::string(name)};
}

int report(const prim::Status &s, char *message, size_t size) {
  if (s) return 0;
  std::snprintf(message, size, "%s", s.message().c_str());
  return 1;
}

template <int W> uint64_t arithmetic(int op, int dst, fp::Rnd mode, int scale, uint64_t a, uint64_t b, uint64_t c) {
  switch (op) {
    case 0: return fp::add<W>(a, b, mode);
    case 1: return fp::mul<W>(a, b, mode, scale);
    case 2: return fp::fma<W>(a, b, c, mode);
    case 3: return dst == 16 ? fp::convert<W, 16>(a, mode) : dst == 32 ? fp::convert<W, 32>(a, mode) : fp::convert<W, 64>(a, mode);
    case 4: return fp::lt<W>(a, b);
    case 5: return fp::le<W>(a, b);
    case 6: return fp::eq<W>(a, b);
    default: return fp::isnan<W>(a);
  }
}
}  // namespace

extern "C" {
// Primitive `name` on nargs arguments of 32 lanes each; returns nonzero with the runtime's message on failure.
int sass_primitive(const char *name, const prim::Warp *warp, uint32_t need, const uint64_t *args, int nargs, uint64_t *out,
                   char *message, size_t size) {
  return report(call(name, *warp, need, args, nargs, out), message, size);
}

int sass_memory_descriptor(uint64_t descriptor, char *message, size_t size) {
  return report(prim::memory_descriptor(descriptor), message, size);
}

// op: 0 add, 1 mul, 2 fma, 3 convert to `dst` bits, 4 lt, 5 le, 6 eq, 7 nan; mode: RN RZ RP RM.
uint64_t sass_fp(int op, int width, int dst, int mode, int scale, uint64_t a, uint64_t b, uint64_t c) {
  const auto m = fp::Rnd(mode);
  return width == 16 ? arithmetic<16>(op, dst, m, scale, a, b, c) : width == 32 ? arithmetic<32>(op, dst, m, scale, a, b, c)
                                                                                : arithmetic<64>(op, dst, m, scale, a, b, c);
}

uint32_t sass_reduced_sine(uint32_t offset) { return mufu::reduced_sine(offset); }
int64_t sass_mufu_coefficient(int segment, int k) { return mufu::COEFFICIENTS[segment][k]; }
}
