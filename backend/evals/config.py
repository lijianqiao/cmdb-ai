"""eval 的路径与库地址解析。

实现流程：
1. eval 跟开发环境必须完全隔离——它每轮开头要清库、重灌种子、重写磁盘文件，
   是整套工具里破坏性最强的一环。任何一处路径算错，毁的都是你的开发数据。
   所以「eval 用哪个库、写哪个目录」集中在这一个模块里，别的模块一律不许自己拼。
2. `apply_env()` 是给入口用的：它把算出来的路径写进环境变量。
   **必须在 import 任何 app.* 之前调用**——`knowledge_storage.KNOWLEDGE_ROOT`
   是模块级常量，import 那一刻就固化了，之后再改环境变量完全没用，
   而失效的表现是 eval 开始写你的真实知识库目录。
3. 这个模块刻意不建目录、不连库、不写文件：纯计算才好测，也才敢在测试里直接
   断言「eval 的目录不在开发目录底下」——换成有副作用的实现，那条断言本身
   就得先把目录建出来才能跑。
"""

import asyncio
import os
import selectors
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core.config import BACKEND_ROOT

# 端口刻意用 5434，跟开发库的 5433 错开：光看连接串就能认出连的是哪个库。
DEFAULT_EVAL_DATABASE_URL = (
    "postgresql+psycopg://evaluser:eval-only@localhost:5434/ent-agent-eval"
)


@dataclass(frozen=True, slots=True)
class EvalPaths:
    """一轮 eval 用到的全部路径，统一挂在 workdir 下面。"""

    workdir: Path
    knowledge_root: Path
    knowledge_trash_root: Path
    results_dir: Path
    baseline_path: Path
    cases_dir: Path
    fixtures_dir: Path


def eval_paths() -> EvalPaths:
    """算出 eval 的全部路径。纯计算，不碰文件系统。

    baseline.json 与 cases/ 刻意放在 workdir **外面**：它们要进版本库，
    而 workdir 是每轮都会被推平重建的。
    """
    evals_dir = BACKEND_ROOT / "evals"
    workdir = evals_dir / ".workdir"
    return EvalPaths(
        workdir=workdir,
        knowledge_root=workdir / "knowledge",
        knowledge_trash_root=workdir / "knowledge_trash",
        results_dir=workdir / "results",
        baseline_path=evals_dir / "baseline.json",
        cases_dir=evals_dir / "cases",
        fixtures_dir=evals_dir / "fixtures",
    )


def eval_database_url() -> str:
    """eval 专用库地址。默认指向 compose 里 profile=eval 的独立容器。"""
    return os.getenv("EVAL_DATABASE_URL") or DEFAULT_EVAL_DATABASE_URL


def loop_factory() -> asyncio.AbstractEventLoop:
    """建一个 psycopg 能用的事件循环。

    Windows 默认的 ProactorEventLoop 跑不了 psycopg 的 async 模式，会直接抛
    `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。
    必须换成 SelectorEventLoop。

    传给 `asyncio.run(main(), loop_factory=loop_factory)`，而不是去改全局
    event loop policy——那个 API 已经废弃了。
    与 tests/test_knowledge_chunk_search_postgres.py 里的做法保持一致。
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.SelectorEventLoop()


def apply_env() -> EvalPaths:
    """把 eval 的库地址与目录写进环境变量，返回路径集。

    **调用时机：必须在 import 任何 app.* 模块之前。**
    knowledge_storage.KNOWLEDGE_ROOT 是模块级常量，import 时就读环境变量了。
    """
    paths = eval_paths()
    os.environ["DATABASE_URL"] = eval_database_url()
    os.environ["KNOWLEDGE_ROOT"] = str(paths.knowledge_root)
    os.environ["KNOWLEDGE_TRASH_ROOT"] = str(paths.knowledge_trash_root)
    return paths
