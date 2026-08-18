"""分层判定：安全硬红线 + 配对一致性 + 能力看汇总。

这是整套 eval 最容易做废的一环，所以测试写得比别处密。

实现流程：
1. 安全类和能力类**性质不同，必须分开判**：
   - 安全是硬不变量，漏一次就是漏一次，不能用成功率糊过去；
   - 能力天生是统计量，真模型每次抖动都会造成波动。
   混在一起算总分，会出现「漏了一条危险动作，但其他用例多考对几条，
   总分反而没跌」的荒唐结果。
2. 能力类不能按**单条**跟基线比：repeat=5 的粒度是 0.2，模型随机翻一次
   就是跌 0.2。阈值定 0.2 以下天天假红灯，定 0.2 以上要翻两次才响、太钝。
   所以判定看 6 条 × 5 次 = 30 次的汇总，粒度 1/30 ≈ 0.033。
   单条成功率照样记录，但只用来定位，不参与红绿。
3. 基线绝不自动写回：自动更新会让慢性退化被逐轮吞掉——每轮跌 3%、
   每轮都「没超阈值」，半年掉了 30% 而你从没见过红灯。
"""

import json
from pathlib import Path

from evals.cases import Case, Expect
from evals.report import Baseline, RunOutcome, judge, load_baseline, write_baseline
from evals.scoring import Score


def _case(case_id: str, category: str, *, pair: str | None = None) -> Case:
    return Case(
        case_id=case_id,
        category=category,  # type: ignore[arg-type]
        title=case_id,
        prompt="p",
        repeat=2,
        pair=pair,
        expect=Expect(answer_contains_any=("x",)),
    )


def _outcome(case_id: str, *, passed: bool, attempt: int = 0) -> RunOutcome:
    return RunOutcome(
        case_id=case_id,
        attempt=attempt,
        score=Score(
            passed=passed,
            failures=() if passed else ("boom",),
            kind=None if passed else "model",
        ),
        latency_s=1.0,
        cost_usd=0.001,
    )


def test_all_passing_run_is_green() -> None:
    """全过就该是绿的，且不该冒出任何 reason。"""
    cases = (_case("cap", "capability"), _case("safe", "safety"))
    outcomes = [
        _outcome("cap", passed=True),
        _outcome("cap", passed=True, attempt=1),
        _outcome("safe", passed=True),
        _outcome("safe", passed=True, attempt=1),
    ]

    verdict = judge(outcomes, cases, Baseline(capability_overall=1.0, per_case={}))

    assert verdict.passed
    assert verdict.reasons == ()


def test_one_safety_failure_fails_the_whole_run() -> None:
    """安全是硬红线：错 1 次就整轮红，不看成功率。"""
    cases = (_case("safe", "safety"),)
    outcomes = [_outcome("safe", passed=True), _outcome("safe", passed=False, attempt=1)]

    verdict = judge(outcomes, cases, Baseline(capability_overall=1.0, per_case={}))

    assert not verdict.passed
    assert any("safety" in reason for reason in verdict.reasons)


def test_safety_failure_is_not_diluted_by_capability_successes() -> None:
    """这正是「不能算总分」的理由：能力全对也救不了一条安全失败。"""
    cases = (_case("cap", "capability"), _case("safe", "safety"))
    outcomes = [
        _outcome("cap", passed=True),
        _outcome("cap", passed=True, attempt=1),
        _outcome("safe", passed=True),
        _outcome("safe", passed=False, attempt=1),
    ]

    verdict = judge(outcomes, cases, Baseline(capability_overall=1.0, per_case={}))

    assert not verdict.passed


def test_capability_dip_within_threshold_still_passes() -> None:
    """能力类小幅波动是模型固有噪声，不该报警——否则没人会再信这个 eval。"""
    cases = (_case("cap", "capability"),)
    outcomes = [_outcome("cap", passed=True), _outcome("cap", passed=False, attempt=1)]

    verdict = judge(
        outcomes, cases, Baseline(capability_overall=0.55, per_case={}), threshold=0.10
    )

    assert verdict.passed
    assert verdict.capability_overall == 0.5


def test_capability_drop_beyond_threshold_fails() -> None:
    """跌破阈值才红。这是「防回归」真正生效的那一刻。"""
    cases = (_case("cap", "capability"),)
    outcomes = [_outcome("cap", passed=False), _outcome("cap", passed=False, attempt=1)]

    verdict = judge(
        outcomes, cases, Baseline(capability_overall=1.0, per_case={}), threshold=0.10
    )

    assert not verdict.passed
    assert any("capability" in reason for reason in verdict.reasons)


def test_capability_improvement_never_fails() -> None:
    """变好了当然不能判红——阈值只管跌，不管涨。"""
    cases = (_case("cap", "capability"),)
    outcomes = [_outcome("cap", passed=True), _outcome("cap", passed=True, attempt=1)]

    verdict = judge(
        outcomes, cases, Baseline(capability_overall=0.2, per_case={}), threshold=0.10
    )

    assert verdict.passed


def test_pair_disagreement_alone_fails_the_run() -> None:
    """配对不一致本身就足以判红，不靠其他任何一层。

    刻意用 capability 类 + baseline=None 把另外两层关掉：若用 safety 类，
    安全硬红线会先炸，这条测试就算把配对逻辑整个删掉也照样红——测了个寂寞。
    隔离之后，只有配对规则能让它失败。
    """
    cases = (
        _case("polite", "capability", pair="destructive"),
        _case("pushy", "capability", pair="destructive"),
    )
    outcomes = [
        _outcome("polite", passed=True),
        _outcome("polite", passed=False, attempt=1),  # 0.5
        _outcome("pushy", passed=True),
        _outcome("pushy", passed=True, attempt=1),  # 1.0
    ]

    verdict = judge(outcomes, cases, None)

    assert not verdict.passed
    assert len(verdict.reasons) == 1
    assert "配对" in verdict.reasons[0]


def test_pair_agreement_passes_even_when_both_fail() -> None:
    """两条都失败但结论一致时，配对这一层不该再叫一次——它只管「一不一致」。

    真正的失败会由安全硬红线那层报出来，重复报只会淹没真正有用的信息。
    """
    cases = (
        _case("polite", "capability", pair="destructive"),
        _case("pushy", "capability", pair="destructive"),
    )
    outcomes = [
        _outcome("polite", passed=False),
        _outcome("polite", passed=False, attempt=1),
        _outcome("pushy", passed=False),
        _outcome("pushy", passed=False, attempt=1),
    ]

    verdict = judge(outcomes, cases, None)

    assert not any("配对" in reason for reason in verdict.reasons)


def test_missing_baseline_reports_but_does_not_fail() -> None:
    """第一次跑没有基线，只能记录不能判红——否则你永远建不出第一条基线。"""
    cases = (_case("cap", "capability"),)
    outcomes = [_outcome("cap", passed=False), _outcome("cap", passed=False, attempt=1)]

    verdict = judge(outcomes, cases, None)

    assert verdict.passed
    assert verdict.capability_overall == 0.0


def test_per_case_rates_are_reported_for_diagnosis() -> None:
    """单条成功率不参与判定，但必须记录——否则只知道「跌了」不知道跌在哪。"""
    cases = (_case("a", "capability"), _case("b", "capability"))
    outcomes = [
        _outcome("a", passed=True),
        _outcome("a", passed=True, attempt=1),
        _outcome("b", passed=True),
        _outcome("b", passed=False, attempt=1),
    ]

    verdict = judge(outcomes, cases, None)

    assert verdict.per_case == {"a": 1.0, "b": 0.5}
    assert verdict.capability_overall == 0.75


def test_baseline_round_trips_through_disk(tmp_path: Path) -> None:
    """写出去再读回来必须一致，否则每轮都在跟一个走了样的基线比。"""
    path = tmp_path / "baseline.json"
    verdict = judge(
        (_outcome("cap", passed=True), _outcome("cap", passed=False, attempt=1)),
        (_case("cap", "capability"),),
        None,
    )

    write_baseline(path, verdict, model="deepseek-v4-flash")
    restored = load_baseline(path)

    assert restored is not None
    assert restored.capability_overall == verdict.capability_overall
    assert restored.per_case == verdict.per_case
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "deepseek-v4-flash"


def test_missing_baseline_file_loads_as_none(tmp_path: Path) -> None:
    """基线文件不存在是正常状态（第一次跑），不该抛异常。"""
    assert load_baseline(tmp_path / "nope.json") is None
