"""Tests for the per-run budget tracker (app.agent.budget)."""

import pytest

from app.agent.budget import Budget, BudgetExceededError


def test_record_cost_accumulates_tokens_by_model() -> None:
    """token 与花费按模型键分组累计，供界面显示整轮用量。"""
    budget = Budget(max_steps=5, max_cost_usd=1.0)

    budget.record_cost(0.01, model_key="local-chat", prompt_tokens=120, completion_tokens=30)
    budget.record_cost(0.02, model_key="local-chat", prompt_tokens=200, completion_tokens=45)

    assert budget.prompt_tokens_used == 320
    assert budget.completion_tokens_used == 75
    assert budget.cost_used_usd == pytest.approx(0.03)
    assert budget.usage_by_model["local-chat"].prompt_tokens == 320
    assert budget.usage_by_model["local-chat"].completion_tokens == 75
    assert budget.usage_by_model["local-chat"].cost_usd == pytest.approx(0.03)


def test_record_cost_keeps_tokens_of_the_call_that_broke_the_budget() -> None:
    """超支那次调用的 token 也已经耗掉了，必须计入，否则界面显示的用量偏小。"""
    budget = Budget(max_steps=5, max_cost_usd=0.01)

    with pytest.raises(BudgetExceededError):
        budget.record_cost(
            0.05, model_key="local-chat", prompt_tokens=900, completion_tokens=100
        )

    assert budget.prompt_tokens_used == 900
    assert budget.completion_tokens_used == 100
    assert budget.usage_by_model["local-chat"].cost_usd == pytest.approx(0.05)


def test_record_cost_without_model_key_skips_breakdown() -> None:
    """没给模型键时只累总数，不往分组里塞一个假的键。"""
    budget = Budget(max_steps=5, max_cost_usd=1.0)

    budget.record_cost(0.01, prompt_tokens=10, completion_tokens=5)

    assert budget.prompt_tokens_used == 10
    assert budget.usage_by_model == {}


@pytest.mark.parametrize("bad_tokens", [(-1, 0), (0, -1)])
def test_record_cost_rejects_negative_tokens(bad_tokens: tuple[int, int]) -> None:
    budget = Budget()
    prompt_tokens, completion_tokens = bad_tokens

    with pytest.raises(ValueError):
        budget.record_cost(
            0.0, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )


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
