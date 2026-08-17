"""Per-run budget tracking and enforcement (docs/guide.md §9.1).

The loop must stop, not retry, when a limit is exceeded — `record_step`
raises rather than silently clamping so the caller can only proceed by
catching the error, never by accident.
"""

import math
from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """Raised when a budget limit is exceeded; the loop must stop, not retry."""

    def __init__(self, limit_name: str, limit: float, used: float) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.used = used
        super().__init__(f"budget exceeded: {limit_name} used {used} > limit {limit}")


@dataclass(slots=True)
class ModelUsage:
    """某个模型键在本次运行中的累计用量。

    分档模型落地后，一轮里会同时出现便宜档和强档，按模型键分组才说得清钱花在哪。
    现在全项目只有 local-chat 一个 chat 键，这个 dict 里也就只有一项。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Budget:
    """Mutable per-run budget tracker; defaults match docs/AGENT_ARCHITECTURE.md §10."""

    max_steps: int = 20
    max_cost_usd: float = 1.0
    steps_used: int = field(default=0, init=False)
    cost_used_usd: float = field(default=0.0, init=False)
    prompt_tokens_used: int = field(default=0, init=False)
    completion_tokens_used: int = field(default=0, init=False)
    usage_by_model: dict[str, ModelUsage] = field(default_factory=dict, init=False)

    def reserve_step(self) -> None:
        """Reserve one model iteration before incurring its external cost."""
        attempted = self.steps_used + 1
        if attempted > self.max_steps:
            raise BudgetExceededError("max_steps", self.max_steps, attempted)
        self.steps_used = attempted

    def record_cost(
        self,
        cost_usd: float,
        *,
        model_key: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Charge one completed model response and stop after crossing the limit.

        **token 先累加再判超支**：超支时这次调用的钱已经花掉了，token 也已经耗掉了，
        丢掉它会让界面上显示的用量小于真实值。
        """
        if not math.isfinite(cost_usd) or cost_usd < 0:
            raise ValueError("cost_usd must be a finite non-negative number")
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("token counts must be non-negative")

        self.prompt_tokens_used += prompt_tokens
        self.completion_tokens_used += completion_tokens
        self.cost_used_usd += cost_usd
        if model_key is not None:
            entry = self.usage_by_model.setdefault(model_key, ModelUsage())
            entry.prompt_tokens += prompt_tokens
            entry.completion_tokens += completion_tokens
            entry.cost_usd += cost_usd

        if self.cost_used_usd > self.max_cost_usd:
            raise BudgetExceededError("max_cost_usd", self.max_cost_usd, self.cost_used_usd)

    def record_step(self, cost_usd: float = 0.0) -> None:
        """Backward-compatible combined operation used by existing callers/tests."""
        self.reserve_step()
        self.record_cost(cost_usd)
