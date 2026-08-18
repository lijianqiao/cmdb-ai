"""知识库根目录必须能被环境变量覆盖，否则 eval 灌种子会写进开发用的目录。

实现流程：
1. `kb_grep` 是真的去磁盘上跑 ripgrep 的，所以 eval 的种子数据必须往磁盘写
   真实的 .md 文件。而根目录原本写死在 `BACKEND_ROOT / "knowledge"`，
   eval 一跑就会把你已经上传的文档搅乱。
2. `_resolve_root` 是纯函数：给它环境变量名和默认值，返回最终生效的路径。
   把这一步单独抽出来，是为了能在不 reload 整个模块的前提下测试它——
   模块级常量在 import 那一刻就固化了，测试里再改环境变量已经晚了。
3. 两条用例分别覆盖「环境变量没设，回落默认值」和「设了，用它」。
"""

from pathlib import Path

import pytest

from app.services import knowledge_storage


def test_resolve_root_falls_back_to_default_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量没设时必须返回默认路径——生产行为不能因为这次改动而变。"""
    monkeypatch.delenv("EVAL_PROBE_ROOT", raising=False)
    default = Path("/tmp/default-knowledge")

    assert knowledge_storage._resolve_root("EVAL_PROBE_ROOT", default) == default


def test_resolve_root_prefers_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量设了就必须用它——这正是 eval 隔离开发目录所依赖的行为。"""
    monkeypatch.setenv("EVAL_PROBE_ROOT", "/tmp/eval-knowledge")

    resolved = knowledge_storage._resolve_root(
        "EVAL_PROBE_ROOT", Path("/tmp/default-knowledge")
    )

    assert resolved == Path("/tmp/eval-knowledge")


def test_empty_env_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空字符串要当成「没设」，否则会解析出当前工作目录，静默写错地方。"""
    monkeypatch.setenv("EVAL_PROBE_ROOT", "")
    default = Path("/tmp/default-knowledge")

    assert knowledge_storage._resolve_root("EVAL_PROBE_ROOT", default) == default
