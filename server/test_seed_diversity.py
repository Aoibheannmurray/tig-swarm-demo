"""Self-running tests for seed-pool diversity admission.

Run directly: `python server/test_seed_diversity.py` (no pytest in this repo).
"""

import seed_diversity as sd

# Two structurally different algorithms (low similarity), and a near-duplicate.
GREEDY = """
use super::*;
fn solve_challenge(c: &Challenge) -> Solution {
    let mut items: Vec<usize> = (0..c.n).collect();
    items.sort_by_key(|&i| c.weight[i]);
    let mut taken = vec![];
    for i in items { if fits(i) { taken.push(i); } }
    Solution { taken }
}
"""

DP = """
use super::*;
fn solve_challenge(c: &Challenge) -> Solution {
    let mut table = vec![vec![0i64; c.budget + 1]; c.n + 1];
    for i in 1..=c.n {
        for w in 0..=c.budget {
            table[i][w] = best_of(table[i-1][w], take(i, w));
        }
    }
    reconstruct(table)
}
"""

GREEDY_VARIANT = GREEDY.replace("c.weight[i]", "c.value[i]")  # near-dup of GREEDY


def test_similarity_self_is_one():
    assert sd.similarity(GREEDY, GREEDY) == 1.0
    print("PASS test_similarity_self_is_one")


def test_different_algorithms_are_dissimilar():
    s = sd.similarity(GREEDY, DP)
    assert s < 0.5, s
    print(f"PASS test_different_algorithms_are_dissimilar (sim={s:.2f})")


def test_near_duplicate_is_similar():
    s = sd.similarity(GREEDY, GREEDY_VARIANT)
    assert s > 0.6, s
    print(f"PASS test_near_duplicate_is_similar (sim={s:.2f})")


def test_loc_counts_source_only():
    code = "use super::*;\n\n// a comment\nfn f() {}\n/* block\n comment */\n"
    assert sd.loc(code) == 2, sd.loc(code)
    print("PASS test_loc_counts_source_only")


def test_admit_first_seed():
    d = sd.decide_admission(GREEDY, [], pool_size=10, similarity_threshold=0.6, max_loc=200)
    assert d.admit and d.evict_index is None and d.reason == "admit"
    print("PASS test_admit_first_seed")


def test_admit_dissimilar():
    d = sd.decide_admission(DP, [GREEDY], pool_size=10, similarity_threshold=0.6, max_loc=200)
    assert d.admit and d.evict_index is None
    print("PASS test_admit_dissimilar")


def test_reject_near_duplicate():
    d = sd.decide_admission(GREEDY_VARIANT, [GREEDY], pool_size=10, similarity_threshold=0.6, max_loc=200)
    assert not d.admit and d.reason == "too_similar"
    print("PASS test_reject_near_duplicate")


def test_reject_too_complex():
    big = "use super::*;\n" + "\n".join(f"let v{i} = {i};" for i in range(300))
    d = sd.decide_admission(big, [], pool_size=10, similarity_threshold=0.6, max_loc=200)
    assert not d.admit and d.reason == "too_complex"
    print("PASS test_reject_too_complex")


def test_full_pool_evicts_most_redundant_not_lowest_score():
    # Pool is full with two near-duplicates (A, A') and one distinct (DP).
    # Admitting a new distinct algorithm should evict one of the redundant pair,
    # never the distinct DP — and the decision is independent of score.
    seeds = [GREEDY, GREEDY_VARIANT, DP]
    new_distinct = """
    use super::*;
    fn solve_challenge(c: &Challenge) -> Solution {
        let mut rng = seeded(c.seed);
        anneal(&mut rng, c, |s| perturb(s))
    }
    """
    d = sd.decide_admission(new_distinct, seeds, pool_size=3, similarity_threshold=0.6, max_loc=200)
    assert d.admit and d.evict_index in (0, 1), d
    print(f"PASS test_full_pool_evicts_most_redundant (evict idx={d.evict_index})")


if __name__ == "__main__":
    test_similarity_self_is_one()
    test_different_algorithms_are_dissimilar()
    test_near_duplicate_is_similar()
    test_loc_counts_source_only()
    test_admit_first_seed()
    test_admit_dissimilar()
    test_reject_near_duplicate()
    test_reject_too_complex()
    test_full_pool_evicts_most_redundant_not_lowest_score()
    print("\nAll seed_diversity tests passed.")
