// CUDA device code uses intrinsics; emulator builds explicitly keep the reference.
#pragma once
#include "sass/softfloat.hpp"
#ifdef __CUDA_ARCH__
#include "sass/fp_cuda.hpp"
namespace sass { namespace fp = fp_cuda; }
#elif defined(SASS_FP_SOFT)
namespace sass { namespace fp = softfloat; }
#else
#include "sass/fp_host.hpp"
namespace sass { namespace fp = fp_host; }
#endif
