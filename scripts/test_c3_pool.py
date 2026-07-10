"""Tests for the fleet-wide C3 slot pool (c3_pool.py).

Runs standalone (`python3 test_c3_pool.py` from the scripts dir) — no network,
no C3. Covers the pure coordination logic:

  * the cap is never exceeded under concurrent acquirers,
  * slots are granted strictly first-come-first-served (lowest ticket first),
  * a crashed holder's slot is reclaimed (dead-PID reap),
  * a lease is released even if the body raises,
  * the no-pool-dir fallback is an equivalent in-process semaphore,
  * get_pool returns a cached per-directory singleton.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c3_pool


SCRIPTS_DIR = str(Path(__file__).resolve().parent)


def _dead_pid() -> int:
    """A PID that is definitely not alive: spawn a trivial process and reap it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _max_overlap(intervals) -> int:
    """Peak number of simultaneously-open [enter, exit) intervals."""
    events = []
    for t0, t1 in intervals:
        events.append((t0, 1))
        events.append((t1, -1))
    events.sort(key=lambda e: (e[0], e[1]))  # exits (-1) before enters at a tie
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak


# Worker body for the cross-process test: acquire the shared pool repeatedly,
# recording each lease's [enter, exit) wall-clock window to a JSON file.
_WORKER_SRC = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import c3_pool
root, out, size, cycles = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
pool = c3_pool.C3SlotPool(size, __import__("pathlib").Path(root))
spans = []
for _ in range(cycles):
    with pool.lease():
        t0 = time.time()
        time.sleep(0.01)
        spans.append((t0, time.time()))
open(out, "w").write(json.dumps(spans))
"""


def test_cap_never_exceeded():
    with tempfile.TemporaryDirectory() as tmp:
        pool = c3_pool.C3SlotPool(3, Path(tmp))
        state = {"active": 0, "max": 0, "done": 0}
        lock = threading.Lock()

        def worker():
            with pool.lease():
                with lock:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.03)
                with lock:
                    state["active"] -= 1
                    state["done"] += 1

        threads = [threading.Thread(target=worker) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert state["done"] == 24, state
        assert state["max"] <= 3, state["max"]
        assert state["max"] >= 1, state["max"]
        # All tickets released -> both dirs empty.
        assert not os.listdir(Path(tmp) / "queue"), os.listdir(Path(tmp) / "queue")
        assert not os.listdir(Path(tmp) / "active"), os.listdir(Path(tmp) / "active")
    print("PASS test_cap_never_exceeded")


def test_cap_never_exceeded_across_processes():
    # The real target: separate OS processes (real agents) sharing one pool dir.
    # Each records its lease windows; the parent asserts no instant had more than
    # `size` leases open at once.
    size, nprocs, cycles = 3, 8, 6
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pool"
        outs = [str(Path(tmp) / f"out-{i}.json") for i in range(nprocs)]
        procs = [
            subprocess.Popen([sys.executable, "-c", _WORKER_SRC, SCRIPTS_DIR,
                              str(root), out, str(size), str(cycles)])
            for out in outs
        ]
        for p in procs:
            assert p.wait(timeout=60) == 0, "worker process failed"
        spans = []
        for out in outs:
            spans.extend((t0, t1) for t0, t1 in json.loads(Path(out).read_text()))
        assert len(spans) == nprocs * cycles, len(spans)
        peak = _max_overlap(spans)
        assert peak <= size, f"cap exceeded across processes: peak={peak} > {size}"
        assert peak >= 2, f"expected real concurrency, peak={peak}"
    print(f"PASS test_cap_never_exceeded_across_processes (peak={peak}/{size})")


def test_fcfs_lowest_ticket_first():
    with tempfile.TemporaryDirectory() as tmp:
        pool = c3_pool.C3SlotPool(1, Path(tmp))
        # Two waiters claim tickets in order; ticket 0 must win before ticket 1.
        t0, p0 = pool._claim_ticket()
        t1, p1 = pool._claim_ticket()
        assert (t0, t1) == (0, 1), (t0, t1)

        # Higher ticket cannot jump the queue while the lower one waits.
        assert pool._try_grant(t1, p1) is None
        # Lowest ticket is granted.
        active0 = pool._try_grant(t0, p0)
        assert active0 is not None
        # Cap is full (size 1) -> ticket 1 still waits even though it's now lowest queued.
        assert pool._try_grant(t1, p1) is None
        # Release ticket 0's slot; now ticket 1 is granted.
        os.unlink(str(active0))
        active1 = pool._try_grant(t1, p1)
        assert active1 is not None
        os.unlink(str(active1))
    print("PASS test_fcfs_lowest_ticket_first")


def test_reaps_dead_holder():
    with tempfile.TemporaryDirectory() as tmp:
        pool = c3_pool.C3SlotPool(1, Path(tmp))
        # Simulate a crashed agent that left an active lease behind, filling the
        # only slot. A live waiter must reclaim it rather than block forever.
        stale = pool._active / "000000000000"
        stale.write_text(str(_dead_pid()))

        granted = {"ok": False}

        def worker():
            with pool.lease():
                granted["ok"] = True

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "lease blocked on a dead holder's slot"
        assert granted["ok"]
    print("PASS test_reaps_dead_holder")


def test_lease_released_on_exception():
    with tempfile.TemporaryDirectory() as tmp:
        pool = c3_pool.C3SlotPool(1, Path(tmp))

        class Boom(Exception):
            pass

        try:
            with pool.lease():
                raise Boom()
        except Boom:
            pass
        # The slot must be free again — dirs empty, and a fresh lease succeeds.
        assert not os.listdir(Path(tmp) / "active")
        assert not os.listdir(Path(tmp) / "queue")
        with pool.lease():
            pass
    print("PASS test_lease_released_on_exception")


def test_in_process_fallback_caps():
    pool = c3_pool.get_pool(2, None)  # no dir -> in-process semaphore
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def worker():
        with pool.lease():
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert state["max"] <= 2, state["max"]
    print("PASS test_in_process_fallback_caps")


def test_get_pool_caches_per_dir():
    with tempfile.TemporaryDirectory() as tmp:
        a = c3_pool.get_pool(3, tmp)
        b = c3_pool.get_pool(3, tmp)
        assert a is b
        assert isinstance(a, c3_pool.C3SlotPool)
    none1 = c3_pool.get_pool(2, None)
    none2 = c3_pool.get_pool(2, None)
    assert none1 is none2
    print("PASS test_get_pool_caches_per_dir")


def _main():
    test_cap_never_exceeded()
    test_cap_never_exceeded_across_processes()
    test_fcfs_lowest_ticket_first()
    test_reaps_dead_holder()
    test_lease_released_on_exception()
    test_in_process_fallback_caps()
    test_get_pool_caches_per_dir()
    print("\nAll C3 pool tests passed.")


if __name__ == "__main__":
    _main()
