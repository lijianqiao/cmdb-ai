"""Tests for the per-run budget tracker (app.agent.budget)."""

import pytest

from app.agent.budget import Budget, BudgetExceededError


def test_record_step_accumulates_usage() -> None:
    budget = Budget(max_steps=5, max_cost_usd=1.0)

    budget.record_step(cost_usd=0.1)
    budget.record_step(cost_usd=0.2)

    assert budget.steps_used == 2
    assert budget.cost_used_usd == pytest.approx(0.3)


def test_record_step_raises_when_max_steps_exceeded() -> None:
    budget = Budget(max_steps=1, max_cost_usd=100.0)
    budget.record_step()

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_step()

    assert exc_info.value.limit_name == "max_steps"


def test_record_step_raises_when_max_cost_exceeded() -> None:
    budget = Budget(max_steps=100, max_cost_usd=0.5)

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_step(cost_usd=0.6)

    assert exc_info.value.limit_name == "max_cost_usd"


def test_default_limits_match_docs_agent_architecture() -> None:
    budget = Budget()

    assert budget.max_steps == 20
    assert budget.max_cost_usd == 1.0


def test_reserve_step_and_record_cost_enforce_limits_separately() -> None:
    budget = Budget(max_steps=2, max_cost_usd=0.50)

    budget.reserve_step()
    budget.reserve_step()

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.reserve_step()
    assert exc_info.value.limit_name == "max_steps"
    assert budget.steps_used == 2

    budget.record_cost(0.30)

    with pytest.raises(BudgetExceededError) as exc_info:
        budget.record_cost(0.21)
    assert exc_info.value.limit_name == "max_cost_usd"
    assert budget.cost_used_usd == pytest.approx(0.51)


@pytest.mark.parametrize("invalid_cost", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_record_cost_rejects_invalid_values(invalid_cost: float) -> None:
    budget = Budget()

    with pytest.raises(ValueError, match="finite non-negative"):
        budget.record_cost(invalid_cost)

    assert budget.cost_used_usd == 0.0
