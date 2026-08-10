"""Per-run budget tracking and enforcement (docs/guide.md §9.1).

The loop must stop, not retry, when a limit is exceeded — `record_step`
raises rather than silently clamping so the caller can only proceed by
catching the error, never by accident.
"""

from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """Raised when a budget limit is exceeded; the loop must stop, not retry."""

    def __init__(self, limit_name: str, limit: float, used: float) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.used = used
        super().__init__(f"budget exceeded: {limit_name} used {used} > limit {limit}")


@dataclass
class Budget:
    """Mutable per-run budget tracker; defaults match docs/AGENT_ARCHITECTURE.md §10."""

    max_steps: int = 20
    max_cost_usd: float = 1.0
    steps_used: int = field(default=0, init=False)
    cost_used_usd: float = field(default=0.0, init=False)

    def record_step(self, cost_usd: float = 0.0) -> None:
        """Record one loop iteration's cost, raising if any limit is now exceeded."""
        self.steps_used += 1
        self.cost_used_usd += cost_usd
        if self.steps_used > self.max_steps:
            raise BudgetExceededError("max_steps", self.max_steps, self.steps_used)
        if self.cost_used_usd > self.max_cost_usd:
            raise BudgetExceededError("max_cost_usd", self.max_cost_usd, self.cost_used_usd)
