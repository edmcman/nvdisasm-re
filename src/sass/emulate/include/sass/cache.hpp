#pragma once
#include <vector>
#include "sass/primitives.hpp"

namespace sass {
struct CacheRequest { uint64_t address; uint32_t operation; };

inline prim::Status cache_controls(uint32_t operation, uint64_t address) {
  if (operation > 4) return {nullptr, "unresolved cache operation"};
  if (operation == 4 && address) return {nullptr, "whole-cache invalidation has no address"};
  if (operation == 3 && address % 128) return {nullptr, "cache priority address must be 128-byte aligned"};
  return {};
}

struct CacheState {
  std::vector<CacheRequest> requests;
  uint64_t generation = 0;
  prim::Status issue(uint32_t operation, uint64_t address) {
    auto status = cache_controls(operation, address);
    if (status) {
      generation += operation == 4;
      requests.push_back({address, operation});
    }
    return status;
  }
};
}
