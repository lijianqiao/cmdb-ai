"""切片逻辑：纯函数，不调 embedding 服务，所以能放进普通 pytest。

实现流程：
1. kb_semantic_search 走的是 knowledge_chunks 表里的向量，所以种子必须把
   6 份文档切成片、每片算一个向量存进去。
2. 算向量要连本机 embedding 服务，那部分没法在 CI 里跑；但**怎么切**是纯逻辑，
   可以也应该单独测。切错了的后果很实在：切出空片会让 embedding 请求直接报错，
   丢字则等于知识库缺内容、检索永远找不到那一段。
"""

from evals.seed import split_into_chunks


def test_short_text_stays_one_chunk() -> None:
    """短文档不该被切碎，否则检索命中的片段上下文全断了。"""
    assert split_into_chunks("一段很短的正文", max_chars=800) == ["一段很短的正文"]


def test_long_text_is_split_and_nothing_is_lost() -> None:
    """切完拼回去必须还是原文——丢字等于知识库缺内容。"""
    text = "段落。" * 500

    chunks = split_into_chunks(text, max_chars=100)

    assert len(chunks) > 1
    assert "".join(chunks) == text


def test_no_empty_chunks() -> None:
    """空片会让 embedding 请求报错，必须在切片阶段就滤掉。"""
    text = "首段\n\n\n\n\n\n\n\n\n\n\n\n次段"

    chunks = split_into_chunks(text, max_chars=5)

    assert chunks
    assert all(chunk.strip() for chunk in chunks)


def test_blank_text_yields_no_chunks() -> None:
    """全空白的文档不该产出任何片——产出了就是一次注定失败的 embedding 请求。"""
    assert split_into_chunks("   \n\n  ", max_chars=10) == []


def test_every_seed_document_produces_at_least_one_chunk() -> None:
    """真实种子文档必须都能切出片，否则那份文档在向量检索里等于不存在。"""
    from evals import config, seed

    contents = seed.read_fixtures(config.eval_paths())

    for doc in seed.SEED_DOCUMENTS:
        chunks = split_into_chunks(contents[doc.doc_id].decode("utf-8"))
        assert chunks, f"{doc.filename} 切不出任何片"
