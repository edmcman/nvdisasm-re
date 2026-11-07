#pragma once
#include <array>
#include <vector>
#include <cstdint>

namespace sass {
struct AsyncCopy { uint64_t address, low, high; uint32_t size; };
struct AsyncState {
  std::vector<AsyncCopy> open, ready;
  std::array<std::vector<std::vector<AsyncCopy>>, 6> groups;
  uint32_t pending = 0;

  void commit(uint32_t board) {
    groups[board].push_back(std::move(open));
    open.clear();
    pending |= 1u << board;
  }
  void wait(uint32_t board, uint32_t count) {
    auto &queue = groups[board];
    size_t completed = queue.size() > count ? queue.size() - count : 0;
    for (size_t i = 0; i < completed; ++i)
      ready.insert(ready.end(), queue[i].begin(), queue[i].end());
    queue.erase(queue.begin(), queue.begin() + completed);
    if (queue.empty()) pending &= ~(1u << board);
  }
  void waits(uint32_t mask) {
    mask &= pending;
    if (!mask) return;
    for (uint32_t board = 0; board < 6; ++board) if (mask >> board & 1) wait(board, 0);
  }
};
}
