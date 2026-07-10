"""Fleet-wide, first-come-first-served slot pool for C3 machine jobs.

Every agent process that shares ONE C3 API key — a single contributor's fleet:
the `run_fleet.py` worktrees locally, or one per-contributor clone on the hosted
runner — draws from the SAME C3 concurrent-job budget. That budget is the C3
plan's cap (`c3_max_parallel_jobs`). Nothing coordinated it before: each agent
process independently ran up to the cap, so N agents burst to N x cap live jobs
and blew past the plan. This module enforces a single shared cap across those
sibling processes.

Model: a global pool of `size` slots. A benchmark deploys its shards by asking
the pool for a slot each; when the pool is full, requests wait in a strict FCFS
queue and a freed slot is handed to the longest waiter. No daemon and no network
— coordination is entirely through a shared directory on the one filesystem all
the fleet's agents can see (`C3_POOL_DIR`, set by `run_fleet.py`). A lone
`run.py` agent is a single process, so an in-process semaphore is equivalent and
is used when no pool directory is configured.

FCFS is a filesystem ticket lock:
  * A waiter claims a strictly increasing integer ticket by exclusive-create
    (`os.open(O_CREAT | O_EXCL)`), which is atomic on POSIX and Windows. The
    ticket number — not the PID — is the create target, so every ticket is
    globally unique across processes; the file's contents record the PID.
  * A slot is granted when the waiter holds the LOWEST outstanding ticket AND
    fewer than `size` leases are active. Granting is an atomic rename from the
    `queue/` dir to the `active/` dir; releasing unlinks the active file.
  * Crash safety: a waiter reaps `queue/` and `active/` entries whose recorded
    PID is dead before every grant check, so an agent that dies mid-job never
    wedges a slot forever. PID recycling can briefly mask a dead holder; that
    self-heals when the recycling process exits and is far rarer than a plain
    crash, which this reclaims immediately.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Cross-process mutex for the grant decision. flock (POSIX) / msvcrt (Windows)
# are both released automatically when the fd closes or the process dies, so a
# crash mid-decision never wedges the lock — no stale-lock reclaim needed.
try:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock_fd(fd: int) -> None:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)  # LK_LOCK gives up after ~10s; keep retrying

    def _unlock_fd(fd: int) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

# Poll cadence while waiting for a slot. Shards run for minutes, so sub-second
# scheduling latency is irrelevant; the jitter keeps sibling waiters from
# re-scanning the pool dir in lockstep.
_POLL_MIN_SECS = 0.05
_POLL_MAX_SECS = 0.25

# Deterministic-but-per-waiter poll jitter without Math.random-style global
# state: a small LCG seeded from pid so concurrent waiters de-synchronise.
_JITTER_LOCK = threading.Lock()
_jitter_state = os.getpid() & 0xFFFFFFFF


def _poll_delay() -> float:
    global _jitter_state
    with _JITTER_LOCK:
        _jitter_state = (1103515245 * _jitter_state + 12345) & 0x7FFFFFFF
        frac = _jitter_state / 0x7FFFFFFF
    return _POLL_MIN_SECS + frac * (_POLL_MAX_SECS - _POLL_MIN_SECS)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Conservative on uncertainty: only report dead
    when we are sure (no such process), so a live long-running job is never
    reaped out from under itself."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # uncertain — do not reap
    return True


class _InProcessPool:
    """Single-process fallback: a plain semaphore. Used when no shared pool
    directory is configured (a lone `run.py`), where there are no siblings to
    coordinate with and file-based FCFS would be pure overhead."""

    def __init__(self, size: int):
        self._sem = threading.Semaphore(size)

    @contextmanager
    def lease(self):
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()


class C3SlotPool:
    """Cross-process FCFS pool of `size` C3 job slots, coordinated through
    `root`. Thread-safe and process-safe: every :meth:`lease` claims its own
    unique ticket file, so no shared in-memory state is needed."""

    def __init__(self, size: int, root: Path):
        self.size = max(1, int(size))
        self.root = Path(root)
        self._queue = self.root / "queue"
        self._active = self.root / "active"
        self._queue.mkdir(parents=True, exist_ok=True)
        self._active.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / "grant.lock"
        self._counter_path = self.root / "counter"

    @contextmanager
    def _grant_lock(self):
        """Serialize the grant decision across every process AND thread sharing
        this pool, so 'count active + promote' is atomic — the invariant that
        keeps the cap from being overshot. Held only for the brief decision,
        never for a lease's lifetime."""
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_fd(fd)
            try:
                yield
            finally:
                _unlock_fd(fd)
        finally:
            os.close(fd)

    # ── directory scans ────────────────────────────────────────────
    @staticmethod
    def _tickets(d: Path) -> list[int]:
        out = []
        for entry in os.scandir(d):
            try:
                out.append(int(entry.name))
            except ValueError:
                continue  # ignore stray files
        return out

    def _reap(self) -> None:
        """Remove queue/active entries whose owning PID is gone."""
        for d in (self._queue, self._active):
            for entry in os.scandir(d):
                try:
                    pid = int(Path(entry.path).read_text().strip() or "0")
                except (OSError, ValueError):
                    continue
                if not _pid_alive(pid):
                    try:
                        os.unlink(entry.path)
                    except FileNotFoundError:
                        pass  # a sibling reaped it first

    # ── ticket lifecycle ───────────────────────────────────────────
    def _claim_ticket(self) -> tuple[int, Path]:
        """Reserve the next ticket from a strictly monotonic counter, under the
        grant lock. Monotonic (never reused) means a ticket number can never
        collide with an in-flight active lease — the bug that a max()-of-the-dirs
        scheme risks — so FCFS ordering stays sound and no waiter is starved."""
        pid = str(os.getpid()).encode()
        with self._grant_lock():
            try:
                n = int(self._counter_path.read_text() or "0")
            except (OSError, ValueError):
                n = 0
            self._counter_path.write_text(str(n + 1))
            path = self._queue / f"{n:012d}"
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, pid)
            finally:
                os.close(fd)
            return n, path

    def _try_grant(self, ticket: int, path: Path) -> Path | None:
        """Grant a slot iff this ticket is the lowest waiter and there is room.
        Returns the active-dir path on success, else None. The whole decision
        runs under the cross-process grant lock, so counting active leases and
        promoting this ticket is one atomic step — no two waiters can both see
        room and both promote."""
        with self._grant_lock():
            self._reap()
            if len(self._tickets(self._active)) >= self.size:
                return None
            queued = self._tickets(self._queue)
            if not queued or min(queued) != ticket:
                return None
            dst = self._active / path.name
            try:
                os.replace(str(path), str(dst))  # atomic promote queue -> active
            except OSError:
                return None
            return dst

    @contextmanager
    def lease(self):
        ticket, path = self._claim_ticket()
        active_path: Path | None = None
        try:
            while active_path is None:
                active_path = self._try_grant(ticket, path)
                if active_path is None:
                    time.sleep(_poll_delay())
            yield
        finally:
            held = active_path if active_path is not None else path
            try:
                os.unlink(str(held))
            except FileNotFoundError:
                pass


# ── process-wide singleton ─────────────────────────────────────────
_POOLS: dict[str, object] = {}
_POOLS_LOCK = threading.Lock()


def get_pool(size: int, pool_dir: str | None = None) -> object:
    """Return the process-wide C3 slot pool. With `pool_dir` set (a fleet), a
    cross-process FCFS :class:`C3SlotPool`; without it (a lone agent), an
    in-process semaphore. Cached per directory so every benchmark and HPO
    candidate in one process shares one pool object."""
    key = pool_dir or ""
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            if pool_dir:
                pool = C3SlotPool(size, Path(pool_dir))
            else:
                pool = _InProcessPool(max(1, int(size)))
            _POOLS[key] = pool
        return pool
