"""
@Author: li
@Email: lijianqiao2906@live.com
@FileName: turn_admission.py
@DateTime: 2026-09-22
@Docs: 根 turn 并发准入：全进程与单个用户同时在跑的对话轮数上限（R5）。

实现流程：
1. 发消息接口在认领会话租约之前先占一个名额，请求结束（无论成败）时归还。
2. 先看单个用户、再看全进程：一个人开很多会话同时提问时，告诉他是「你自己太多」
   （429），而不是笼统的「服务器忙」（503）。
3. 满了立即拒绝、不排队：排队只会让请求挂住直到 HTTP 超时，用户以为卡死；
   直接返回 Retry-After，由前端提示稍后再试。

为什么需要它：等模型时不再占数据库连接之后，连接池不再是瓶颈，真实瓶颈变成模型端的
并发能力和进程内存；单会话租约只挡同一会话连发，挡不住一个人开很多会话、很多人同时提问。

为什么是进程内计数：本项目强制单 worker（见 app/main.py:validate_single_worker_environment），
与 turn_registry、SpawnManager 同一前提。计数只在同步代码里改，事件循环单线程，不需要锁。
"""

from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings

type AdmissionDecision = Literal["ok", "user_limit", "global_limit"]


@dataclass(slots=True)
class TurnAdmission:
    """进程内正在运行的根 turn 计数。"""

    max_total: int
    max_per_user: int
    _total: int = 0
    _per_user: dict[int, int] = field(default_factory=dict)

    def try_acquire(self, user_id: int) -> AdmissionDecision:
        """为一轮对话占一个名额；返回 ok 才算占到，调用方必须在结束时 release。"""
        running = self._per_user.get(user_id, 0)
        if running >= self.max_per_user:
            return "user_limit"
        if self._total >= self.max_total:
            return "global_limit"
        self._total += 1
        self._per_user[user_id] = running + 1
        return "ok"

    def release(self, user_id: int) -> None:
        """归还 try_acquire 占到的名额。"""
        running = self._per_user.get(user_id, 0)
        if running <= 1:
            self._per_user.pop(user_id, None)
        else:
            self._per_user[user_id] = running - 1
        self._total = max(0, self._total - 1)


turn_admission = TurnAdmission(
    max_total=settings.AGENT_MAX_CONCURRENT_TURNS,
    max_per_user=settings.AGENT_MAX_CONCURRENT_TURNS_PER_USER,
)
