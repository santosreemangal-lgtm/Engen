"""Small, dependency-free latency measurement utilities.

This module measures local elapsed time only. It does not submit transactions,
perform network requests, or claim end-to-end Solana execution latency.
"""

from contextlib import contextmanager
import math
from threading import Lock
from time import perf_counter_ns


class LatencyRecorder:
    """Collect bounded timing samples by stage for local performance tests."""

    def __init__(self, max_samples=10000):
        if (
            not isinstance(max_samples, int)
            or isinstance(max_samples, bool)
            or max_samples < 1
        ):
            raise ValueError("max_samples must be a positive integer.")
        self._max_samples = max_samples
        self._samples = {}
        self._lock = Lock()

    @contextmanager
    def measure(self, stage):
        """Measure one code block and record elapsed milliseconds."""
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("A non-empty latency stage name is required.")
        started = perf_counter_ns()
        try:
            yield
        finally:
            elapsed_ms = (perf_counter_ns() - started) / 1_000_000
            self.record(stage, elapsed_ms)

    def record(self, stage, elapsed_ms):
        """Record an already measured duration in milliseconds."""
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("A non-empty latency stage name is required.")
        if (
            not isinstance(elapsed_ms, (int, float))
            or isinstance(elapsed_ms, bool)
            or not math.isfinite(elapsed_ms)
            or elapsed_ms < 0
        ):
            raise ValueError("elapsed_ms must be a non-negative number.")

        with self._lock:
            samples = self._samples.setdefault(stage, [])
            if len(samples) < self._max_samples:
                samples.append(float(elapsed_ms))

    @staticmethod
    def _percentile(sorted_samples, percentile):
        if len(sorted_samples) == 1:
            return sorted_samples[0]
        index = round((len(sorted_samples) - 1) * percentile)
        return sorted_samples[index]

    def summary(self):
        """Return count, mean, max and nearest-rank percentile timings."""
        with self._lock:
            snapshot = {
                stage: list(samples)
                for stage, samples in self._samples.items()
            }

        result = {}
        for stage, samples in snapshot.items():
            if not samples:
                continue
            ordered = sorted(samples)
            result[stage] = {
                "count": len(ordered),
                "mean_ms": round(sum(ordered) / len(ordered), 3),
                "max_ms": round(ordered[-1], 3),
                "p50_ms": round(self._percentile(ordered, 0.50), 3),
                "p95_ms": round(self._percentile(ordered, 0.95), 3),
                "p99_ms": round(self._percentile(ordered, 0.99), 3),
            }
        return result
