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

// Balanced round-robin partition assignment, one thread per node:
// partition[i] = i % num_parts. This is the most size-balanced partition
// possible (part sizes differ by at most one), so it satisfies max_part_size
// whenever any balanced partition does. Edge-cut quality is poor by design —
// the refiner's job is to reduce the cut (move nodes between parts, greedy
// bipartition, multilevel coarsening) while keeping the balance constraint.
extern "C" __global__ void round_robin_partition(
    int* __restrict__ partition,
    const int num_nodes,
    const int num_parts
) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= num_nodes) return;
    partition[i] = (num_parts > 0) ? (i % num_parts) : 0;
}
