"""加载并严格校验 YAML 用例。

实现流程：
1. 用例做成数据文件而不是 Python 代码，好处是加用例不用写代码；代价是
   **打错字没有编译器帮你抓**。所以这个加载器严到近乎啰嗦：多一个字段报错、
   分节名写错报错、category 写错报错、expect 全空也报错。它在替编译器把关。
2. 「expect 全空」这条最要紧：一条什么都不断言的安全用例会永远 PASS，
   让你以为危险动作的红线还守着，其实早就空了。这种 bug 不报错、不变红，
   只会让你对着一片绿灯把回归发出去。宁可当场炸。
3. id 重复同理：report 的成绩按 case_id 做键，重复会让两条用例互相覆盖，
   基线从此对不上却毫无征兆。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

_TOP_LEVEL_FIELDS = frozenset(
    {"id", "category", "title", "prompt", "repeat", "pair", "expect"}
)
_EXPECT_SECTIONS = frozenset({"outcome", "invariants", "efficiency"})
_OUTCOME_FIELDS = frozenset({"answer_contains_any", "answer_not_contains"})
_INVARIANT_FIELDS = frozenset(
    {"must_call_any", "must_not_call", "must_create_proposal", "must_not_execute"}
)
_EFFICIENCY_FIELDS = frozenset({"max_steps"})

DEFAULT_REPEAT = 5


class InvalidCaseError(ValueError):
    """用例文件不合法。消息里一定带上文件路径，好定位是哪份写错了。"""


@dataclass(frozen=True, slots=True)
class Expect:
    """一条用例的全部断言，按三层摊平。"""

    answer_contains_any: tuple[str, ...] = ()
    answer_not_contains: tuple[str, ...] = ()
    must_call_any: tuple[str, ...] = ()
    must_not_call: tuple[str, ...] = ()
    must_create_proposal: bool = False
    must_not_execute: bool = False
    max_steps: int | None = None

    def is_empty(self) -> bool:
        """什么都不断言的 expect 会永远 PASS，必须被拦下。"""
        return not (
            self.answer_contains_any
            or self.answer_not_contains
            or self.must_call_any
            or self.must_not_call
            or self.must_create_proposal
            or self.must_not_execute
            or self.max_steps is not None
        )


@dataclass(frozen=True, slots=True)
class Case:
    """一条用例。`pair` 非空时参与措辞配对一致性检查。"""

    case_id: str
    category: Literal["capability", "safety"]
    title: str
    prompt: str
    repeat: int
    pair: str | None
    expect: Expect


def _reject_unknown(
    section: dict[str, Any], allowed: frozenset[str], *, path: Path, where: str
) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise InvalidCaseError(f"{path}: {where} 里有不认识的字段 {sorted(unknown)}")


def _as_str_tuple(raw: Any, *, path: Path, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise InvalidCaseError(f"{path}: {field} 必须是字符串列表")
    return tuple(raw)


def _as_section(raw: Any, *, path: Path, name: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidCaseError(f"{path}: expect.{name} 必须是一个映射")
    return raw


def load_case(path: Path) -> Case:
    """读一份用例 YAML，校验后返回 Case；任何不合法都抛 InvalidCaseError。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidCaseError(f"{path}: 顶层必须是一个映射")
    _reject_unknown(raw, _TOP_LEVEL_FIELDS, path=path, where="顶层")

    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise InvalidCaseError(f"{path}: id 必须是非空字符串")

    category = raw.get("category")
    if category not in ("capability", "safety"):
        raise InvalidCaseError(
            f"{path}: category 必须是 capability 或 safety，得到 {category!r}"
        )

    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidCaseError(f"{path}: prompt 必须是非空字符串")

    repeat = raw.get("repeat", DEFAULT_REPEAT)
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
        raise InvalidCaseError(f"{path}: repeat 必须是 >= 1 的整数，得到 {repeat!r}")

    expect_raw = raw.get("expect")
    if not isinstance(expect_raw, dict):
        raise InvalidCaseError(f"{path}: expect 必须是一个映射")
    _reject_unknown(expect_raw, _EXPECT_SECTIONS, path=path, where="expect")

    outcome = _as_section(expect_raw.get("outcome"), path=path, name="outcome")
    invariants = _as_section(expect_raw.get("invariants"), path=path, name="invariants")
    efficiency = _as_section(expect_raw.get("efficiency"), path=path, name="efficiency")
    _reject_unknown(outcome, _OUTCOME_FIELDS, path=path, where="expect.outcome")
    _reject_unknown(invariants, _INVARIANT_FIELDS, path=path, where="expect.invariants")
    _reject_unknown(efficiency, _EFFICIENCY_FIELDS, path=path, where="expect.efficiency")

    expect = Expect(
        answer_contains_any=_as_str_tuple(
            outcome.get("answer_contains_any"), path=path, field="answer_contains_any"
        ),
        answer_not_contains=_as_str_tuple(
            outcome.get("answer_not_contains"), path=path, field="answer_not_contains"
        ),
        must_call_any=_as_str_tuple(
            invariants.get("must_call_any"), path=path, field="must_call_any"
        ),
        must_not_call=_as_str_tuple(
            invariants.get("must_not_call"), path=path, field="must_not_call"
        ),
        must_create_proposal=bool(invariants.get("must_create_proposal", False)),
        must_not_execute=bool(invariants.get("must_not_execute", False)),
        max_steps=efficiency.get("max_steps"),
    )
    if expect.is_empty():
        raise InvalidCaseError(
            f"{path}: expect 一条断言都没有，这样的用例会永远 PASS"
        )

    pair = raw.get("pair")
    if pair is not None and not isinstance(pair, str):
        raise InvalidCaseError(f"{path}: pair 必须是字符串")

    return Case(
        case_id=case_id,
        category=category,
        title=str(raw.get("title", "")),
        prompt=prompt,
        repeat=repeat,
        pair=pair,
        expect=expect,
    )


def load_all_cases(cases_dir: Path) -> tuple[Case, ...]:
    """按文件名排序加载全部用例，保证每轮执行顺序稳定，两轮日志才能对照着看。"""
    cases = tuple(load_case(path) for path in sorted(cases_dir.glob("*.yaml")))

    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise InvalidCaseError(
                f"{cases_dir}: 用例 id 重复 {case.case_id!r}——"
                f"报告按 id 归并成绩，重复会让两条用例互相覆盖"
            )
        seen.add(case.case_id)
    return cases
