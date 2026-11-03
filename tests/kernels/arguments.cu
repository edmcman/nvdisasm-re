template<int N> struct Bytes { unsigned char data[N]; };

template<class T>
__device__ unsigned int checksum(T value, unsigned int offset) {
    auto bytes = reinterpret_cast<const unsigned char *>(&value);
    unsigned int result = 0;
    #pragma unroll
    for (int i = 0; i < sizeof(T); ++i)
        result += bytes[i] * (offset + i + 1);
    return result;
}

extern "C" __global__ void arguments(
    const unsigned int *input, unsigned int *output,
    unsigned char a, unsigned short b, Bytes<3> c, unsigned int d,
    unsigned long long e, Bytes<12> f, Bytes<16> g) {
    auto i = blockIdx.x * blockDim.x + threadIdx.x;
    output[i] = input[3 * i] + checksum(a, 0) + checksum(b, 1) +
        checksum(c, 3) + checksum(d, 6) + checksum(e, 10) +
        checksum(f, 18) + checksum(g, 30);
}
