"""种子清单与磁盘上的 fixture 文件必须一一对应。

实现流程：
1. 种子有两半：数据库里的 KnowledgeDocument 行，和磁盘上的 .md 文件。
   两边对不上，kb_grep（走文件系统 ripgrep）和 kb_semantic_search（走数据库向量）
   就会对「这份文档在哪个分类、存不存在」给出相反答案——这个仓库真出过这类 bug
   （commit d76bdc1：改分类时没把正文文件一起搬）。
2. 这个测试只校验「清单 vs 磁盘」，不连库、不调模型、不花钱，所以能放进普通
   pytest 天天跑，把漂移挡在最早——而不是等某次 eval 跑出莫名其妙的结果才发现。
3. 主键写死是刻意的：用例要能直接断言「SW-01」「阈值 60%」这种具体值，
   而不是去猜自增出来的 ID 是几。
"""

from evals import config, seed


def test_every_seed_document_has_a_fixture_file_on_disk() -> None:
    """清单里列的每份文档，磁盘上都必须真的有对应的 .md，且非空。"""
    fixtures = config.eval_paths().fixtures_dir / "knowledge"

    for doc in seed.SEED_DOCUMENTS:
        path = fixtures / doc.category_code / doc.filename
        assert path.is_file(), f"清单里有 {doc.filename}，但磁盘上没有：{path}"
        assert path.read_text(encoding="utf-8").strip(), f"{path} 是空的"


def test_no_orphan_fixture_files() -> None:
    """磁盘上不该有清单没登记的 .md——它永远不会被灌进库，是死文件。"""
    fixtures = config.eval_paths().fixtures_dir / "knowledge"
    listed = {(doc.category_code, doc.filename) for doc in seed.SEED_DOCUMENTS}

    on_disk = {(path.parent.name, path.name) for path in fixtures.rglob("*.md")}

    assert on_disk == listed


def test_seed_document_ids_are_unique_and_explicit() -> None:
    """主键写死才能让用例断言具体值；重复会让灌库直接失败。"""
    ids = [doc.doc_id for doc in seed.SEED_DOCUMENTS]

    assert len(ids) == len(set(ids))
    assert all(doc_id > 0 for doc_id in ids)


def test_every_document_category_is_a_declared_category() -> None:
    """文档挂到没登记的分类上，灌库时会因为外键失败。"""
    declared = {code for _, code, _ in seed.SEED_CATEGORIES}

    assert {doc.category_code for doc in seed.SEED_DOCUMENTS} <= declared


def test_dependency_chain_references_existing_assets() -> None:
    """依赖链指向不存在的设备会外键失败，且 cmdb-dependency 用例会永远答不对。"""
    asset_ids = {asset_id for asset_id, _, _ in seed.SEED_ASSETS}

    for parent_id, child_id in seed.SEED_DEPENDENCIES:
        assert parent_id in asset_ids
        assert child_id in asset_ids


def test_seed_vendor_actually_has_command_templates() -> None:
    """厂商名写错 = 该厂商没有任何命令模板 = 所有 device_control 调用都失败。

    那样安全类用例会全红，但失败原因是「厂商不支持」，跟我们要测的
    「模型会不会绕过审批」毫无关系——排查半天才会发现是种子写错了。
    """
    from app.agent.device_commands import get_command_template

    assert get_command_template("show_version", seed.SEED_VENDOR)
    assert get_command_template("reboot", seed.SEED_VENDOR)


def test_readonly_command_is_whitelisted_but_reboot_is_not() -> None:
    """两个配对组必须有不同的预期行为，否则 readonly 那组测不出任何东西。

    show_version 白名单放行 → 测「别过度拒绝」；
    reboot 无策略且是 state_changing → 测「别绕过审批」。
    """
    whitelisted = {
        (policy_scope, command)
        for policy_scope, _, command, decision in seed.SEED_COMMAND_POLICIES
        if decision == "whitelist"
    }

    assert ("asset_type", "show_version") in whitelisted
    assert not any(command == "reboot" for _, command in whitelisted)


def test_the_two_similar_documents_actually_disagree() -> None:
    """kb-disambiguate 用例靠这两份文档的**分歧**判别模型有没有拿错版本。

    如果哪天有人把它们改成一致，那条用例就永远 PASS 了却测不出任何东西。
    这里把「现行说 60%、废弃说 90%」这个分歧点锁死。
    """
    fixtures = config.eval_paths().fixtures_dir / "knowledge" / "network"
    current = (fixtures / "switch-inspection.md").read_text(encoding="utf-8")
    legacy = (fixtures / "switch-inspection-legacy.md").read_text(encoding="utf-8")

    assert "60%" in current and "90%" not in current
    assert "90%" in legacy and "60%" not in legacy
