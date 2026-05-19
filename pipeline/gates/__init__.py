"""Quality gates. Each gate is a small function with a uniform shape."""

from .runner import GateOutcome, GateReport, run_gates

__all__ = ["GateOutcome", "GateReport", "run_gates"]
