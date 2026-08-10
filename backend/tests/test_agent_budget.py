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
