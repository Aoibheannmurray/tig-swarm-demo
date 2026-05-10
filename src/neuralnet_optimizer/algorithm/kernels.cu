#include <stdint.h>
#include <math.h>
#include <float.h>

extern "C" __global__ void adamw_update_kernel(
    const float *params,
    const float *grads,
    float *m,
    float *v,
    float *updates,
    unsigned long long n,
    float lr,
    float beta1,
    float beta2,
    float eps,
    float weight_decay,
    float bias_correction1,
    float bias_correction2
) {
    unsigned long long idx = (unsigned long long)blockIdx.x * (unsigned long long)blockDim.x
                             + (unsigned long long)threadIdx.x;

    if (idx >= n) {
        return;
    }

    float p = params[idx];
    float g = grads[idx];
    float old_m = m[idx];
    float old_v = v[idx];

    if (!isfinite(p) || !isfinite(g) || !isfinite(old_m) || !isfinite(old_v)) {
        m[idx] = 0.0f;
        v[idx] = 0.0f;
        updates[idx] = 0.0f;
        return;
    }

    if (g == 0.0f && old_m == 0.0f && old_v == 0.0f) {
        updates[idx] = 0.0f;
        return;
    }

    float one_minus_beta1 = 1.0f - beta1;
    float one_minus_beta2 = 1.0f - beta2;

    float new_m = beta1 * old_m + one_minus_beta1 * g;
    float new_v = beta2 * old_v + one_minus_beta2 * g * g;

    if (!isfinite(new_m) || !isfinite(new_v) || new_v < 0.0f) {
        m[idx] = 0.0f;
        v[idx] = 0.0f;
        updates[idx] = 0.0f;
        return;
    }

    m[idx] = new_m;
    v[idx] = new_v;

    float bc1 = fmaxf(bias_correction1, 1.0e-16f);
    float bc2 = fmaxf(bias_correction2, 1.0e-16f);

    float m_hat = new_m / bc1;
    float v_hat = new_v / bc2;
    float denom = sqrtf(fmaxf(v_hat, 0.0f)) + eps;

    float adam_term = m_hat / denom;
    float decay_term = weight_decay * p;
    float upd = -lr * (adam_term + decay_term);

    if (!isfinite(upd)) {
        upd = 0.0f;
    }

    updates[idx] = upd;
}