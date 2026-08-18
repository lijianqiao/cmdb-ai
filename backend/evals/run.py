"""eval 入口：重建测试库 → 灌种子 → 逐条跑用例 → 三层打分 → 打印。

    uv run python -m evals.run

实现流程：
1. **第一件事必须是 apply_env()，而且要在 import 任何 app.* 之前。**
   knowledge_storage 的 KNOWLEDGE_ROOT 是模块级常量，import 那一刻就固化了；
   晚一步设环境变量，eval 就会写进你开发用的知识库目录，而且不会报错。
   这也是本文件所有 app.* 的 import 都写在函数体里、而不是文件顶部的原因。
2. 被测入口是 run_chat_turn 而不是 run_loop：前者是前端真正走的那条路
   （含工具装配、HITL gate、预算记账），测它才叫防回归；后者绕过了一半东西。
3. 每次运行开一个**全新会话**。同一条用例跑 N 次之间绝不能共享上下文，
   否则第二次会「记得」第一次的答案，测出来的成功率是假的。
4. 串行跑。并发可行（不同 session_id 互不干扰），但第一版求简单、日志好读。
5. 带一道累计成本熔断：这是个会自动花钱的工具，失控时得有个刹车。
"""

import asyncio
import io
import os
import sys
import time

from evals.config import apply_env, loop_factory

# 必须在任何 app.* import 之前执行，理由见模块 docstring 第 1 条。
_PATHS = apply_env()

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from evals.cases import Case, load_all_cases  # noqa: E402
from evals.report import (  # noqa: E402
    RunOutcome,
    judge,
    load_baseline,
    write_baseline,
)
from evals.scoring import score  # noqa: E402
from evals.trajectory import load_trajectory  # noqa: E402

# 整轮 eval 的成本上限。单轮已被 Budget(max_steps=20) 卡住（实测约 $0.02），
# 这道熔断防的是另一种事故：用例数或 repeat 被改大之后无人看管地跑下去。
MAX_TOTAL_USD = 1.0

EVAL_USERNAME = "evaluser"


async def ensure_eval_user(session: AsyncSession) -> int:
    """建一个 eval 专用超管并返回其 id。

    测试库是推平重建的，里面一个用户都没有，没有用户就起不了会话。
    用超管是因为 Agent 链路上有 `agent:use` 权限门禁，超管绕过它——
    eval 要测的是模型行为，不是权限系统（权限已有专门的单元测试覆盖）。
    """
    from app.core.security import hash_password
    from app.models.user import User

    user = User(
        username=EVAL_USERNAME,
        email="eval@example.invalid",
        # 这个账号只存在于一次性测试库里，永远不会被登录，也不进任何配置文件
        hashed_password=hash_password("eval-only-never-logged-in"),
        is_superuser=True,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return int(user.id)


async def run_case_once(
    session_factory: async_sessionmaker[AsyncSession],
    case: Case,
    *,
    user_id: int,
    attempt: int,
) -> RunOutcome:
    """跑一条用例一次：开新会话 → 发问 → 跑完 → 读轨迹 → 打分。"""
    from app.agent.chat_turn import run_chat_turn
    from app.agent.session import append_user_message
    from app.models.agent_session import AgentSession

    started = time.monotonic()
    async with session_factory() as db:
        agent_session = AgentSession(
            user_id=user_id, title=f"eval:{case.case_id}#{attempt + 1}"
        )
        db.add(agent_session)
        await db.flush()
        session_id = int(agent_session.id)

        boundary = await append_user_message(db, session_id, case.prompt)
        await db.commit()

        # run_chat_turn 不 commit，由调用方负责
        outcome = await run_chat_turn(db, session_id=session_id, actor_user_id=user_id)
        await db.commit()

        trajectory = await load_trajectory(
            db, session_id=session_id, after_message_id=int(boundary.id)
        )

    return RunOutcome(
        case_id=case.case_id,
        attempt=attempt,
        score=score(trajectory, case.expect, loop_reason=outcome.reason),
        latency_s=time.monotonic() - started,
        cost_usd=trajectory.cost_usd,
    )


async def prepare_database() -> tuple[async_sessionmaker[AsyncSession], int]:
    """推平测试库、灌种子与向量、建 eval 用户。返回会话工厂与用户 id。"""
    from evals import seed

    engine = create_async_engine(os.environ["DATABASE_URL"])
    await seed.reset_schema(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        await seed.seed_all(db, _PATHS)
        await seed.seed_embeddings(db, _PATHS)
        user_id = await ensure_eval_user(db)
        await db.commit()
    return session_factory, user_id


async def main() -> int:
    """跑完全部用例并打印结果。返回进程退出码：0 通过，1 有失败。"""
    # 一轮要跑十几分钟。stdout 重定向到文件时 Python 默认按块缓冲，
    # 那样在跑完之前你什么都看不到，没法中途判断是不是卡住了。
    # isinstance 收窄不只是为了过类型检查：stdout 被别的东西接管时
    # （比如 pytest 捕获）确实可能不是 TextIOWrapper，那时跳过即可。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    print(f"测试库：{_PATHS.workdir}")
    session_factory, user_id = await prepare_database()

    cases = load_all_cases(_PATHS.cases_dir)
    outcomes: list[RunOutcome] = []
    total_cost = 0.0

    for case in cases:
        for attempt in range(case.repeat):
            result = await run_case_once(
                session_factory, case, user_id=user_id, attempt=attempt
            )
            outcomes.append(result)
            total_cost += result.cost_usd

            mark = "PASS" if result.score.passed else "FAIL"
            print(
                f"[{mark}] {case.case_id} #{attempt + 1}/{case.repeat}  "
                f"{result.latency_s:.1f}s  ${result.cost_usd:.4f}"
            )
            for failure in result.score.failures:
                print(f"       └─ {failure}")

            if total_cost > MAX_TOTAL_USD:
                print(
                    f"\n[中止] 累计成本 ${total_cost:.4f} 超过上限 ${MAX_TOTAL_USD}，"
                    f"停止本轮以免继续烧钱"
                )
                return 1

    baseline = load_baseline(_PATHS.baseline_path)
    verdict = judge(outcomes, cases, baseline)

    print("\n=== 汇总 ===")
    print(f"capability 汇总成功率：{verdict.overall_pass_rate:.3f}")
    for case_id, rate in sorted(verdict.per_case.items()):
        print(f"  {case_id:30} {rate:.2f}")
    print(f"总成本：${total_cost:.4f}")

    if baseline is None:
        print("（没有基线，本轮只记录不判定。确认成绩合理后用 --update-baseline 建立基线。）")
    else:
        print(f"基线 capability：{baseline.overall_pass_rate:.3f}")

    for reason in verdict.reasons:
        print(f"[FAIL] {reason}")

    if "--update-baseline" in sys.argv:
        # 模型名必须记进基线：分数和阈值都是绑定具体模型的，换了模型这份基线
        # 就不该再用。从 settings 读而不是 os.environ——配置由 pydantic-settings
        # 从 .env 加载，压根不会进环境变量，读 os.environ 只会得到空串。
        from app.core.config import get_settings

        write_baseline(
            _PATHS.baseline_path, verdict, model=get_settings().LLM_CHAT_MODEL
        )
        print(f"已写入基线：{_PATHS.baseline_path}")

    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(), loop_factory=loop_factory))
