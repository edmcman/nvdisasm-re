// C entry points every generated library exports for Python (sass.emulate.native): memory images.
#pragma once
#include <cstdio>
#include "sass/machine.hpp"

extern "C" {
sass::Memory *sass_memory_new(const sass::Memory *base) { auto *m = new sass::Memory; m->base = base; return m; }
void sass_memory_free(sass::Memory *m) { delete m; }
void sass_memory_write(sass::Memory *m, int space, uint64_t address, const uint8_t *data, size_t n) {
  for (size_t i = 0; i < n; ++i) m->write(space, address + i, data[i]);
}
void sass_memory_read(const sass::Memory *m, int space, uint64_t address, uint8_t *data, size_t n) {
  for (size_t i = 0; i < n; ++i) data[i] = m->read(space, address + i);
}
}

namespace sass {
inline int report(const Fault &f, char *message, size_t size) { std::snprintf(message, size, "%s", f.message.c_str()); return 1; }
}
