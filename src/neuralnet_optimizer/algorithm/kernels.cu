#include <stdint.h>
#include <math.h>

extern "C" __global__ void adamw_update_kernel(
    const float* __restrict__ param,
    const float* __restrict__ grad,
    float* __restrict__ m,
    float* __restrict__ v,
    float* __restrict__ update,
    const uint32_t n,
    const float lr,
    const float beta1,
    const float beta2,
    const float bias_correction1,
    const float bias_correction2,
    const float eps,
    const float weight_decay
) {
    const uint32_t idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= n) {
        return;
    }

    const float g = grad[idx];

    const float mt = beta1 * m[idx] + (1.0f - beta1) * g;
    const float vt = beta2 * v[idx] + (1.0f - beta2) * g * g;

    m[idx] = mt;
    v[idx] = vt;

    const float m_hat = mt / bias_correction1;
    const float v_hat = vt / bias_correction2;

    const float adam_step = m_hat / (sqrtf(v_hat) + eps);

    float wd_step = 0.0f;
    if (g != 0.0f) {
        wd_step = weight_decay * param[idx];
    }

    update[idx] = -lr * (adam_step + wd_step);
}