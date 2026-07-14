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
// whenever any balanced partition does. Edge-cut quality is poor by design -
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

// Per-hyperedge part summary for the refinement loop (num_parts == 64):
//   edge_mask[h] : bit p set iff hyperedge h has >= 1 pin in part p
//   edge_solo[h] : bit p set iff hyperedge h has exactly 1 pin in part p
// One thread per hyperedge; each thread writes only its own two slots, so
// multi-block launches are deterministic.
extern "C" __global__ void edge_part_info(
    const int num_hyperedges,
    const int* __restrict__ hyperedge_offsets,
    const int* __restrict__ hyperedge_nodes,
    const int* __restrict__ partition,
    unsigned long long* __restrict__ edge_mask,
    unsigned long long* __restrict__ edge_solo
) {
    for (int h = blockIdx.x * blockDim.x + threadIdx.x; h < num_hyperedges; h += blockDim.x * gridDim.x) {
        const int start = hyperedge_offsets[h];
        const int end = hyperedge_offsets[h + 1];
        unsigned char cnt[64];
        for (int p = 0; p < 64; p++) cnt[p] = 0;
        unsigned long long mask = 0ULL;
        for (int pos = start; pos < end; pos++) {
            const int p = partition[hyperedge_nodes[pos]];
            if (p < 0) continue; // unassigned pin (cluster-growth construction)
            mask |= 1ULL << p;
            if (cnt[p] < 2) cnt[p]++;
        }
        unsigned long long solo = 0ULL;
        for (int p = 0; p < 64; p++) {
            if (cnt[p] == 1) solo |= 1ULL << p;
        }
        edge_mask[h] = mask;
        edge_solo[h] = solo;
    }
}

// Best single-node move under the connectivity metric, one thread per node.
// Moving node j from part p to part q changes the metric by
//   -(# incident edges where j is the sole pin in p)      [p leaves the edge]
//   +(# incident edges that do not touch q at all)        [q joins the edge]
// so gain(q) = solo_p - (deg - present[q]). Writes (best_gain, best_target)
// to thread-private slots; ties broken by smallest q => deterministic.
// `dir` restricts targets to q > p (dir=1) or q < p (dir=0); alternating the
// direction between rounds prevents pairwise A<->B oscillation when moves are
// applied in parallel. Balance is enforced by compute_accept/apply_moves.
extern "C" __global__ void node_best_move(
    const int num_nodes,
    const int dir,
    const int* __restrict__ node_offsets,
    const int* __restrict__ node_hyperedges,
    const int* __restrict__ partition,
    const unsigned long long* __restrict__ edge_mask,
    const unsigned long long* __restrict__ edge_solo,
    int* __restrict__ best_gain,
    int* __restrict__ best_target
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        const int p = partition[j];
        const int start = node_offsets[j];
        const int end = node_offsets[j + 1];
        int present[64];
        for (int q = 0; q < 64; q++) present[q] = 0;
        int solo_p = 0;
        const int deg = end - start;
        for (int pos = start; pos < end; pos++) {
            const int h = node_hyperedges[pos];
            solo_p += (int)((edge_solo[h] >> p) & 1ULL);
            unsigned long long mm = edge_mask[h];
            while (mm) {
                const int q = __ffsll((long long)mm) - 1;
                mm &= mm - 1;
                present[q]++;
            }
        }
        int bq = -1;
        int bg = 0; // only strictly-positive gains are reported
        for (int q = 0; q < 64; q++) {
            if (q == p) continue;
            if (((q > p) ? 1 : 0) != dir) continue;
            const int g = solo_p - (deg - present[q]);
            if (g > bg) {
                bg = g;
                bq = q;
            }
        }
        best_gain[j] = bg;
        best_target[j] = bq;
    }
}

// ---- Fully GPU-resident move application (no host sorting / O(n) download).
//
// Round pipeline after node_best_move:
//   1. gain_histogram : per-part histogram of candidate gains (clamped to 31)
//                       + per-part count of candidates leaving it.
//   2. compute_accept : per target part q, find the minimal gain threshold
//                       t_q such that ALL candidates with gain >= t_q fit in
//                       q's remaining capacity (all-or-nothing per gain level
//                       => deterministic, no ticket races). Also block all
//                       out-moves from a part that could otherwise empty.
//   3. apply_moves    : node j moves iff gain>0, not out-blocked, and its
//                       clamped gain >= t_{target}. Partition writes are
//                       thread-private; size/moved updates are commutative
//                       integer atomics, so results are order-independent.

extern "C" __global__ void gain_histogram(
    const int num_nodes,
    const int* __restrict__ partition,
    const int* __restrict__ best_gain,
    const int* __restrict__ best_target,
    unsigned int* __restrict__ gain_hist,  // [64 * 32], zeroed by host
    unsigned int* __restrict__ out_count   // [64], zeroed by host
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        const int g = best_gain[j];
        const int q = best_target[j];
        if (g <= 0 || q < 0) continue;
        const int gc = g > 31 ? 31 : g;
        atomicAdd(&gain_hist[q * 32 + gc], 1u);
        atomicAdd(&out_count[partition[j]], 1u);
    }
}

// One block, 64 threads (one per part). Deterministic: each thread reads the
// full (already synchronized) histogram and writes only its own two slots.
extern "C" __global__ void compute_accept(
    const int max_part_size,
    const int* __restrict__ part_sizes,     // [64]
    const unsigned int* __restrict__ gain_hist,
    const unsigned int* __restrict__ out_count,
    int* __restrict__ accept_thresh,        // [64]
    int* __restrict__ out_block             // [64]
) {
    const int q = threadIdx.x;
    if (q >= 64) return;
    int cap = max_part_size - part_sizes[q];
    if (cap < 0) cap = 0;
    unsigned int cnt = 0;
    int thresh = 32; // nothing accepted unless capacity admits a full level
    for (int g = 31; g >= 1; g--) {
        const unsigned int hg = gain_hist[q * 32 + g];
        if (cnt + hg <= (unsigned int)cap) {
            cnt += hg;
            thresh = g;
        } else {
            break;
        }
    }
    accept_thresh[q] = thresh;
    // Conservative: actual out-movers <= candidates, so this never lets a
    // part empty. In-moves only add nodes, never remove.
    out_block[q] = ((int)part_sizes[q] - (int)out_count[q] < 1) ? 1 : 0;
}

// ---- Seeded cluster-growth construction ------------------------------------
//
// Alternative to recursive bisection: nodes start unassigned (partition[j] ==
// -1) except 64 seed nodes, one per part. Per round:
//   node_affinity    : each unassigned node counts, per part q, how many of
//                      its incident hyperedges already touch q (edge_mask from
//                      edge_part_info, which skips unassigned pins) and
//                      reports its best (count, part). Thread-private writes;
//                      ties broken by smallest q => deterministic.
//   assign_histogram : per-part histogram of candidate affinities (clamped to
//                      31). compute_accept (shared with refinement, out_count
//                      zeroed) turns it into a per-part affinity threshold
//                      whose full accepted cohort provably fits capacity.
//   apply_assign     : unassigned candidates at/above their target's threshold
//                      join it; size/assigned updates are commutative atomics.

extern "C" __global__ void node_affinity(
    const int num_nodes,
    const int* __restrict__ node_offsets,
    const int* __restrict__ node_hyperedges,
    const int* __restrict__ partition,
    const unsigned long long* __restrict__ edge_mask,
    int* __restrict__ best_gain,
    int* __restrict__ best_target
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        if (partition[j] >= 0) {
            best_gain[j] = 0;
            best_target[j] = -1;
            continue;
        }
        int present[64];
        for (int q = 0; q < 64; q++) present[q] = 0;
        const int start = node_offsets[j];
        const int end = node_offsets[j + 1];
        for (int pos = start; pos < end; pos++) {
            unsigned long long mm = edge_mask[node_hyperedges[pos]];
            while (mm) {
                const int q = __ffsll((long long)mm) - 1;
                mm &= mm - 1;
                present[q]++;
            }
        }
        int bq = -1;
        int bg = 0; // affinity must be >= 1 to be a candidate this round
        for (int q = 0; q < 64; q++) {
            if (present[q] > bg) {
                bg = present[q];
                bq = q;
            }
        }
        best_gain[j] = bg;
        best_target[j] = bq;
    }
}

extern "C" __global__ void assign_histogram(
    const int num_nodes,
    const int* __restrict__ best_gain,
    const int* __restrict__ best_target,
    unsigned int* __restrict__ gain_hist   // [64 * 32], zeroed by host
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        const int g = best_gain[j];
        const int q = best_target[j];
        if (g <= 0 || q < 0) continue;
        const int gc = g > 31 ? 31 : g;
        atomicAdd(&gain_hist[q * 32 + gc], 1u);
    }
}

extern "C" __global__ void apply_assign(
    const int num_nodes,
    int* __restrict__ partition,
    const int* __restrict__ best_gain,
    const int* __restrict__ best_target,
    const int* __restrict__ accept_thresh, // [64]
    int* __restrict__ part_sizes,          // [64], updated atomically
    unsigned int* __restrict__ assigned    // [1], zeroed by host
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        if (partition[j] >= 0) continue;
        const int g = best_gain[j];
        const int q = best_target[j];
        if (g <= 0 || q < 0) continue;
        const int gc = g > 31 ? 31 : g;
        if (gc < accept_thresh[q]) continue;
        partition[j] = q;
        atomicAdd(&part_sizes[q], 1);
        atomicAdd(assigned, 1u);
    }
}

extern "C" __global__ void apply_moves(
    const int num_nodes,
    int* __restrict__ partition,
    const int* __restrict__ best_gain,
    const int* __restrict__ best_target,
    const int* __restrict__ accept_thresh,  // [64]
    const int* __restrict__ out_block,      // [64]
    int* __restrict__ part_sizes,           // [64], updated atomically
    unsigned int* __restrict__ moved        // [1], zeroed by host
) {
    for (int j = blockIdx.x * blockDim.x + threadIdx.x; j < num_nodes; j += blockDim.x * gridDim.x) {
        const int g = best_gain[j];
        const int q = best_target[j];
        if (g <= 0 || q < 0) continue;
        const int p = partition[j];
        if (out_block[p]) continue;
        const int gc = g > 31 ? 31 : g;
        if (gc < accept_thresh[q]) continue;
        partition[j] = q;
        atomicAdd(&part_sizes[q], 1);
        atomicSub(&part_sizes[p], 1);
        atomicAdd(moved, 1u);
    }
}
