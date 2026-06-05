// IMPORTANT NOTES:
// 1. You can import any libraries available in nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04
//    Example:
//    #include <curand_kernel.h>
//    #include <stdint.h>
//    #include <math.h>
//    #include <float.h>
//
// 2. If you launch a kernel with multiple blocks, any writes should be to non-overlapping parts of the memory
//    Example:
//    arr[blockIdx.x] = 1; // This IS deterministic
//    arr[0] = 1; // This is NOT deterministic
//
// 3. Any kernel available in <challenge>.cu will be available here
//
// 4. If you need to use random numbers, you can use the CURAND library and seed it with challenge.seed.
//    Example rust:
//    let d_seed = stream.memcpy_stod(seed)?;
//    stream
//       .launch_builder(&my_kernel)
//       .arg(&d_seed)
//       ...
//
//    Example cuda:
//    extern "C" __global__ void my_kernel(
//        const uint8_t *seed,
//        ...
//    ) {
//        curandState state;
//        curand_init(((uint64_t *)(seed))[0], 0, 0, &state);
//        ...
//    }

// Exact nearest-neighbour search, one thread per query. Each thread scans the
// whole database and keeps the index minimising squared L2 distance. Simple and
// exact (the optimal neighbour for every query); the refiner's job is to make it
// FAST (tiling/shared memory, early-exit pruning, an index/clustering).
extern "C" __global__ void nearest_neighbor_search(
    const float* __restrict__ query_vectors,
    const float* __restrict__ database_vectors,
    int* __restrict__ results,
    const int num_queries,
    const int database_size,
    const int vector_dims
) {
    const int q = blockIdx.x * blockDim.x + threadIdx.x;
    if (q >= num_queries) return;

    const size_t stride = (size_t)vector_dims;
    const float* query = query_vectors + (size_t)q * stride;

    float best_dist = 3.402823466e+38f; // FLT_MAX, no header dependency
    int best_idx = 0;
    for (int d = 0; d < database_size; d++) {
        const float* db_vec = database_vectors + (size_t)d * stride;
        float sum = 0.0f;
        for (int k = 0; k < vector_dims; k++) {
            const float diff = query[k] - db_vec[k];
            sum += diff * diff;
        }
        if (sum < best_dist) {
            best_dist = sum;
            best_idx = d;
        }
    }
    results[q] = best_idx;
}
