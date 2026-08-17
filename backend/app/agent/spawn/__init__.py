"""进程内子 Agent Spawn 运行时。

实现流程：
1. Spawn 前在同一 session 锁内完成角色、父子关系、预算和累计配额校验，非法请求不留回执。
2. 合法请求先持久化 REQUESTED/SPAWNING、独立 child 消息与 spawn trace，再创建进程内 task。
3. 每个 child 使用独立数据库 session 运行受限 Agent loop，并把预算用量和固定失败分类写回终态。
4. wait 只等待而不取消 child；send 只写 RUNNING child 的隔离消息空间；list 始终从 registry 重建快照。
5. close 按后代优先取消 task，有限等待后可强制 detach，并在 session 锁内只释放一次并发槽。

模块划分（原先是单文件 1364 行，按职责拆开，行为完全不变）：

- ``types``：状态常量、回执/预算 dataclass、异常、事件发布协议。依赖底座，不 import 包内其它模块。
- ``receipts``：ORM 注册行 ↔ 不可变回执的转换与完整性校验，纯函数。
- ``admission``：预算合法性与路径深度校验，纯函数。
- ``manager``：SpawnManager 本体与回执 GC 循环，负责生命周期编排与进程内状态。

本文件是门面：外部一律 ``from app.agent.spawn import X``，不要直接 import 子模块，
这样以后再调整内部划分不会波及调用方。
"""

from app.agent.spawn.admission import (
    depth_from_path,
    path_depth,
    validate_child_budget,
)
from app.agent.spawn.manager import (
    SpawnManager,
    run_receipt_gc_loop,
    spawn_manager,
)
from app.agent.spawn.receipts import _budget_payload, _to_receipt
from app.agent.spawn.types import (
    ChildBudgetSnapshot,
    ChildErrorClass,
    ChildNotFoundError,
    ChildReceipt,
    ChildReceiptCorruptionError,
    ChildRunner,
    ChildRunResult,
    ChildRuntimeUnavailableError,
    ChildTerminalStatus,
    ChildWaitTimeoutError,
    NoopSpawnEventPublisher,
    SpawnEventPublisher,
    SpawnRejectedError,
)

__all__ = [
    "ChildBudgetSnapshot",
    "ChildErrorClass",
    "ChildNotFoundError",
    "ChildReceipt",
    "ChildReceiptCorruptionError",
    "ChildRunResult",
    "ChildRunner",
    "ChildRuntimeUnavailableError",
    "ChildTerminalStatus",
    "ChildWaitTimeoutError",
    "NoopSpawnEventPublisher",
    "SpawnEventPublisher",
    "SpawnManager",
    "SpawnRejectedError",
    "depth_from_path",
    "path_depth",
    "run_receipt_gc_loop",
    "spawn_manager",
    "validate_child_budget",
]
