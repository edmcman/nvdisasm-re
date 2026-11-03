#include <cstdio>
#include <cstdint>
#include <stdexcept>
#include <cuda.h>
#include <cupti.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <vector>
#include <cstring>
#include <thread>
#include <exception>

namespace fs = std::filesystem;
static thread_local bool inside = false;
static std::recursive_mutex gate;
static fs::path root;
static std::ofstream events;
static size_t sequence = 0;
static CUpti_SubscriberHandle subscriber;
struct Region { CUdeviceptr address; size_t size; };
struct Launch {
  size_t id;
  std::string name, error;
  std::vector<std::vector<unsigned char>> arguments;
  std::map<CUdeviceptr, Region> regions;
  unsigned grid[3], block[3], shared;
};
static thread_local Launch *pending = nullptr;

static void check(CUresult result, const char *op) {
  if (result != CUDA_SUCCESS) throw std::runtime_error(std::string(op) + ": CUDA " + std::to_string(result));
}
static std::string hex(const std::vector<unsigned char> &data) {
  std::ostringstream s;
  for (auto byte : data) s << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
  return s.str();
}
static void save(const fs::path &path, const void *data, size_t size) {
  std::ofstream file(path, std::ios::binary);
  file.write(static_cast<const char *>(data), size);
  if (!file) throw std::runtime_error("writing " + path.string());
}
static bool region(Launch &l, uint64_t pointer) {
  if (pointer < (1ull << 32) || pointer >> 48 || (pointer & 7)) return false;
  auto it = l.regions.upper_bound(pointer);
  if (it != l.regions.begin()) {
    --it;
    if (pointer - it->first < it->second.size) return false;
  }
  CUdeviceptr base; size_t size;
  if (cuMemGetAddressRange(&base, &size, pointer) != CUDA_SUCCESS) {
    unsigned type = 0;
    if (cuPointerGetAttribute(&type, CU_POINTER_ATTRIBUTE_MEMORY_TYPE, pointer) == CUDA_SUCCESS && type == CU_MEMORYTYPE_DEVICE)
      throw std::runtime_error("device pointer has no capturable allocation range");
    return false;
  }
  l.regions.emplace(base, Region{base, size});
  return true;
}
static void snapshot(Launch &l, const char *phase) {
  std::set<CUdeviceptr> done;
  for (;;) {
    std::vector<Region> todo;
    for (const auto &[p, r] : l.regions) if (!done.count(p)) todo.push_back(r);
    if (todo.empty()) break;
    for (const auto &r : todo) {
      if (r.size > (256ull << 20)) throw std::runtime_error("allocation exceeds 256 MiB capture limit");
      std::vector<unsigned char> bytes(r.size);
      check(cuMemcpyDtoH(bytes.data(), r.address, r.size), "snapshot");
      save(root / (std::to_string(l.id) + "-" + phase + "-" + std::to_string(r.address) + ".bin"), bytes.data(), bytes.size());
      done.insert(r.address);
      if (!std::strcmp(phase, "before"))
        for (size_t i = 0; i + 8 <= bytes.size(); i += 8) {
          uint64_t p; std::memcpy(&p, bytes.data() + i, 8);
          region(l, p);
        }
    }
  }
}
static void finish(Launch &l) {
  events << "{\"id\":" << l.id << ",\"name\":" << std::quoted(l.name) << ",\"error\":" << std::quoted(l.error)
         << ",\"grid\":[" << l.grid[0] << ',' << l.grid[1] << ',' << l.grid[2]
         << "],\"block\":[" << l.block[0] << ',' << l.block[1] << ',' << l.block[2] << "],\"shared\":" << l.shared << ",\"arguments\":[";
  bool comma = false;
  for (const auto &a : l.arguments) { if (comma) events << ','; comma = true; events << std::quoted(hex(a)); }
  events << "],\"regions\":["; comma = false;
  for (const auto &[p, r] : l.regions) { if (comma) events << ','; comma = true; events << "{\"address\":" << p << ",\"size\":" << r.size << '}'; }
  events << "]}\n" << std::flush;
}
// CUPTI permits synchronization in callbacks, but memory/metadata APIs run on a helper thread.
template <class F> static void inspect(CUcontext context, F action) {
  std::exception_ptr error;
  std::thread worker([&] {
    inside = true;
    try {
      check(cuCtxPushCurrent(context), "push capture context");
      try { action(); } catch (...) { error = std::current_exception(); }
      CUcontext popped; check(cuCtxPopCurrent(&popped), "pop capture context");
    } catch (...) { error = std::current_exception(); }
  });
  worker.join();
  if (error) std::rethrow_exception(error);
}
static void CUPTIAPI callback(void *, CUpti_CallbackDomain domain, CUpti_CallbackId id, const void *raw) {
  if (inside) return;
  inside = true;
  try {
    if (domain == CUPTI_CB_DOMAIN_RESOURCE && id == CUPTI_CBID_RESOURCE_MODULE_LOADED) {
      std::lock_guard lock(gate);
      auto resource = static_cast<const CUpti_ResourceData *>(raw);
      auto module = static_cast<const CUpti_ModuleResourceData *>(resource->resourceDescriptor);
      save(root / ("module-" + std::to_string(module->moduleId) + ".cubin"), module->pCubin, module->cubinSize);
    } else if (domain == CUPTI_CB_DOMAIN_DRIVER_API) {
      auto data = static_cast<const CUpti_CallbackData *>(raw);
      bool ordinary = id == CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel || id == CUPTI_DRIVER_TRACE_CBID_cuLaunchKernel_ptsz;
      bool extended = id == CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx || id == CUPTI_DRIVER_TRACE_CBID_cuLaunchKernelEx_ptsz;
      if (!ordinary && !extended) {
        if (data->callbackSite == CUPTI_API_ENTER && std::strstr(data->functionName, "Launch")) {
          std::lock_guard lock(gate);
          events << "{\"unsupported_api\":" << std::quoted(data->functionName) << "}\n" << std::flush;
        }
      } else if (data->callbackSite == CUPTI_API_ENTER) {
        gate.lock();
        pending = new Launch{}; auto &l = *pending; l.id = sequence++;
        CUfunction f; void **params, **extra;
        if (ordinary) {
          auto p = static_cast<const cuLaunchKernel_params *>(data->functionParams);
          f = p->f; params = p->kernelParams; extra = p->extra;
          l.grid[0] = p->gridDimX; l.grid[1] = p->gridDimY; l.grid[2] = p->gridDimZ;
          l.block[0] = p->blockDimX; l.block[1] = p->blockDimY; l.block[2] = p->blockDimZ; l.shared = p->sharedMemBytes;
        } else {
          auto p = static_cast<const cuLaunchKernelEx_params *>(data->functionParams);
          f = p->f; params = p->kernelParams; extra = p->extra; auto c = p->config;
          l.grid[0] = c->gridDimX; l.grid[1] = c->gridDimY; l.grid[2] = c->gridDimZ;
          l.block[0] = c->blockDimX; l.block[1] = c->blockDimY; l.block[2] = c->blockDimZ; l.shared = c->sharedMemBytes;
          if (c->numAttrs) l.error = "extended launch attributes require capture support";
        }
        inspect(data->context, [&] {
        const char *name = nullptr; check(cuFuncGetName(&name, f), "function name"); l.name = name;
        check(cuCtxSynchronize(), "before synchronize");
        const unsigned char *packed = nullptr; size_t packed_size = 0;
        if (extra) for (size_t j = 0; extra[j] != CU_LAUNCH_PARAM_END; j += 2) {
          if (extra[j] == CU_LAUNCH_PARAM_BUFFER_POINTER) packed = static_cast<const unsigned char *>(extra[j + 1]);
          if (extra[j] == CU_LAUNCH_PARAM_BUFFER_SIZE) packed_size = *static_cast<size_t *>(extra[j + 1]);
        }
        for (size_t i = 0; ; ++i) {
          size_t offset, size;
          CUresult status = cuFuncGetParamInfo(f, i, &offset, &size);
          if (status == CUDA_ERROR_INVALID_VALUE) break;
          check(status, "parameter metadata");
          const auto *bytes = params ? static_cast<const unsigned char *>(params[i]) : packed && offset + size <= packed_size ? packed + offset : nullptr;
          if (!bytes) throw std::runtime_error("missing parameter storage");
          l.arguments.emplace_back(bytes, bytes + size);
          for (size_t j = 0; j + 8 <= size; j += 8) { uint64_t p; std::memcpy(&p, bytes + j, 8); region(l, p); }
        }
        snapshot(l, "before");
        });
      } else if (pending) {
        auto &l = *pending;
        inspect(data->context, [&] {
        check(*static_cast<const CUresult *>(data->functionReturnValue), "kernel launch");
        check(cuCtxSynchronize(), "after synchronize");
        snapshot(l, "after");
        });
        finish(l); delete pending; pending = nullptr; gate.unlock();
      }
    }
  } catch (const std::exception &e) {
    if (pending) {
      pending->error = e.what();
      auto data = domain == CUPTI_CB_DOMAIN_DRIVER_API ? static_cast<const CUpti_CallbackData *>(raw) : nullptr;
      if (data && data->callbackSite == CUPTI_API_EXIT) { finish(*pending); delete pending; pending = nullptr; gate.unlock(); }
    } else std::fprintf(stderr, "YOLO capture: %s\n", e.what());
  }
  inside = false;
}
extern "C" int start_capture(const char *directory) {
 try {
  root = directory; fs::create_directories(root); events.open(root / "launches.jsonl");
  auto result = cuptiSubscribe(&subscriber, callback, nullptr);
  if (result != CUPTI_SUCCESS) throw std::runtime_error("CUPTI subscription " + std::to_string(result));
  if (cuptiEnableDomain(1, subscriber, CUPTI_CB_DOMAIN_RESOURCE) != CUPTI_SUCCESS ||
      cuptiEnableDomain(1, subscriber, CUPTI_CB_DOMAIN_DRIVER_API) != CUPTI_SUCCESS)
    throw std::runtime_error("CUPTI callback domains unavailable");
  return 0;
 } catch (const std::exception &e) { std::fprintf(stderr, "YOLO capture: %s\n", e.what()); return 1; }
}
extern "C" void stop_capture() { cuptiUnsubscribe(subscriber); events.flush(); }
