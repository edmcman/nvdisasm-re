// C entry points every generated library exports for Python (sass.emulate.native): memory images.
#pragma once
#include <cstdio>
#include <algorithm>
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
void sass_async_issue(sass::AsyncState *s, uint64_t address, uint64_t low, uint64_t high, uint32_t size) {
  s->open.push_back({address, low, high, size});
}
void sass_async_commit(sass::AsyncState *s, uint32_t board) { s->commit(board); }
void sass_async_wait(sass::AsyncState *s, uint32_t board, uint32_t count, uint32_t mask) {
  if (board < 6) s->wait(board, count);
  s->waits(mask);
}
size_t sass_async_ready(sass::AsyncState *s, sass::AsyncCopy *out, size_t size) {
  if (size < s->ready.size()) return s->ready.size();
  std::copy(s->ready.begin(), s->ready.end(), out);
  size_t count = s->ready.size();
  s->ready.clear();
  return count;
}
}

namespace sass {
inline int report(const Fault &f, char *message, size_t size) { std::snprintf(message, size, "%s", f.message.c_str()); return 1; }
}
