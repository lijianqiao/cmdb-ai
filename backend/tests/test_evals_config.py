"""eval 的路径与库地址解析：必须跟开发环境完全隔离。

实现流程：
1. eval 每轮开头都要清库、重灌种子、重写磁盘文件——它是整套工具里破坏性最强的
   一环。所以它连的库、写的目录都必须跟开发用的分开，而且这件事要有测试盯着。
2. `evals.config` 只做纯计算：不建目录、不连库、不写文件。正因为是纯计算，
   才敢在测试里直接断言「eval 的目录不在开发目录底下」——换成有副作用的实现，
   这条断言本身就得先把目录建出来才能跑。
3. 这三条断言防的是同一类事故：某次重构把路径算错，seed 把开发数据清了。
"""

import pytest

from app.core.config import BACKEND_ROOT
from evals import config


def test_eval_knowledge_root_is_outside_the_real_one() -> None:
    """eval 的知识库目录绝不能落在开发用的 backend/knowledge 里面。"""
    paths = config.eval_paths()
    real_root = (BACKEND_ROOT / "knowledge").resolve()

    resolved = paths.knowledge_root.resolve()
    assert resolved != real_root
    assert real_root not in resolved.parents


def test_eval_database_url_points_at_the_dedicated_container() -> None:
    """默认必须指向 5434 的 eval 专用库，绝不能是开发库的 5433。"""
    url = config.eval_database_url()

    assert "5434" in url
    assert "ent-agent-eval" in url
    assert "5433" not in url


def test_all_eval_artifacts_live_under_one_workdir() -> None:
    """产物集中在一个 workdir 下，才能整体删掉重来而不误伤别的东西。"""
    paths = config.eval_paths()

    assert paths.knowledge_root.is_relative_to(paths.workdir)
    assert paths.knowledge_trash_root.is_relative_to(paths.workdir)
    assert paths.results_dir.is_relative_to(paths.workdir)


def test_database_url_can_be_overridden_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """允许换库地址，否则换台机器跑就得改代码。"""
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgresql+psycopg://u:p@host:1234/db")

    assert config.eval_database_url() == "postgresql+psycopg://u:p@host:1234/db"
