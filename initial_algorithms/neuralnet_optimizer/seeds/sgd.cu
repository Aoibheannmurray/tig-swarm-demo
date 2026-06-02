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

// SGD weight-update kernel. Returns the *update to add* to each parameter
// (the scaffold applies it via `apply_parameter_updates_direct`, i.e.
// params += update): update = -learning_rate * clip(grad, -1, 1). The gradient
// clip keeps a single large gradient from blowing the step up.
extern "C" __global__ void sgd_step(
    const float* __restrict__ gradients,
    const unsigned int n,
    const float learning_rate,
    float* __restrict__ updates
) {
    const unsigned int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < n) {
        // Simple SGD: clip the gradient for stability, then step against it.
        const float g = fminf(fmaxf(gradients[idx], -1.0f), 1.0f);
        updates[idx] = -learning_rate * g;
    }
}
