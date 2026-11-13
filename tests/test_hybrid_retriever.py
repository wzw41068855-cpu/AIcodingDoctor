"""Hybrid Search：固定文档与假重排器，不访问 API 或现有 Chroma。"""

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from rag.hybrid_retriever import HybridCodeRetriever, code_tokens


def chunk(chunk_id, path, symbol, body, line=1):
    return Document(id=chunk_id, page_content=body, metadata={
        "relative_path": path, "source": f"/repo/{path}",
        "module_path": path.removesuffix(".py").replace("/", "."),
        "qualified_name": symbol, "symbol_name": symbol,
        "symbol_names": f'["{symbol}"]', "symbol_type": "function",
        "start_line": line, "end_line": line + body.count("\n"),
    })


class FakeStore:
    def __init__(self, docs):
        self.docs = {doc.id: doc for doc in docs}
        self.get_calls = 0

    def get(self, *, ids, include):
        self.get_calls += 1
        selected = [self.docs[chunk_id] for chunk_id in ids if chunk_id in self.docs]
        return {"ids": [d.id for d in selected],
                "documents": [d.page_content for d in selected],
                "metadatas": [d.metadata for d in selected]}


class FakeManager:
    def __init__(self, docs, dense_ids=()):
        self.vector_store = FakeStore(docs)
        self.dense_ids = dense_ids
        self.manifest = {"files": {
            doc.metadata["relative_path"]: {
                "hash": doc.id, "signature": "v5", "ids": [doc.id]
            } for doc in docs
        }}
        self.search_calls = 0

    def similarity_search(self, query, k):
        self.search_calls += 1
        return [self.vector_store.docs[chunk_id] for chunk_id in self.dense_ids[:k]]


class FakeReranker:
    def __init__(self, order=None, fail=False):
        self.order = order
        self.fail = fail
        self.calls = []

    def rerank(self, documents, query, top_n):
        self.calls.append(([d.id for d in documents], query, top_n))
        if self.fail:
            raise TimeoutError("fake timeout")
        order = self.order if self.order is not None else range(len(documents))
        return [{"index": index, "relevance_score": 1.0}
                for index in list(order)[:top_n]]


def retriever(docs, dense_ids=(), reranker=None):
    manager = FakeManager(docs, dense_ids)
    return HybridCodeRetriever(vector_manager=manager,
                               reranker=reranker or FakeReranker()), manager


def test_identifier_and_chinese_tokenizer():
    tokens = code_tokens("验证 getUserByToken crud/users.py")
    assert {"验证", "getuserbytoken", "get", "user", "by", "token",
            "crud/users.py"} <= set(tokens)


def test_bm25_finds_symbol_and_path_without_changing_source():
    docs = [chunk("a", "crud/users.py", "create_token", "return uuid.uuid4()"),
            chunk("b", "models/user.py", "UserToken.__repr__", "return token"),
            chunk("c", "services/cache.py", "read_cache", "return redis.get(key)")]
    hybrid, manager = retriever(docs, reranker=FakeReranker())
    result = hybrid.invoke("crud/users.py create_token")
    assert result[0].id == "a"
    assert result[0].page_content == "return uuid.uuid4()"
    assert "_hybrid_chunk_id" not in result[0].metadata
    assert manager.search_calls == 1


def test_same_name_different_files_not_merged():
    docs = [chunk("a", "api/users.py", "verify", "return True"),
            chunk("b", "services/users.py", "verify", "return False")]
    hybrid, _ = retriever(docs, dense_ids=("a", "b"))
    assert {d.id for d in hybrid.invoke("verify")} == {"a", "b"}


def test_dense_and_bm25_same_chunk_fused_once():
    docs = [chunk("a", "users.py", "get_user_by_token", "return token")]
    hybrid, _ = retriever(docs, dense_ids=("a",))
    assert [d.id for d in hybrid.invoke("token")] == ["a"]
    assert hybrid.reranker.calls[0][0] == ["a"]


def test_rerank_reorders_candidates_and_preserves_ids():
    docs = [chunk("a", "a.py", "a", "token"),
            chunk("b", "b.py", "b", "password")]
    hybrid, _ = retriever(docs, dense_ids=("a", "b"), reranker=FakeReranker(order=[1, 0]))
    assert [d.id for d in hybrid.invoke("token")] == ["b", "a"]


def test_rerank_failure_falls_back_to_rrf():
    docs = [chunk("a", "a.py", "a", "token")]
    hybrid, _ = retriever(docs, dense_ids=("a",), reranker=FakeReranker(fail=True))
    assert [d.id for d in hybrid.invoke("token")] == ["a"]


def test_zero_lexical_overlap_does_not_add_irrelevant_candidates():
    docs = [chunk("a", "a.py", "alpha", "alpha"),
            chunk("b", "b.py", "beta", "beta")]
    hybrid, _ = retriever(docs, dense_ids=("a",))
    assert [d.id for d in hybrid.invoke("missing_identifier")] == ["a"]


def test_manifest_change_refreshes_bm25_and_removes_old_chunk():
    old = chunk("old", "users.py", "create_token", "old code")
    new = chunk("new", "users.py", "create_token", "new code")
    hybrid, manager = retriever([old])
    assert [d.id for d in hybrid.invoke("create_token")] == ["old"]
    manager.vector_store.docs = {"new": new}
    manager.manifest["files"]["users.py"] = {
        "hash": "new", "signature": "v5", "ids": ["new"]}
    assert [d.id for d in hybrid.invoke("create_token")] == ["new"]
    assert manager.vector_store.get_calls == 2


def test_no_rebuild_on_unchanged_manifest_and_empty_index():
    doc = chunk("a", "users.py", "create_token", "return token")
    hybrid, manager = retriever([doc])
    hybrid.invoke("token")
    hybrid.invoke("token")
    assert manager.vector_store.get_calls == 1
    manager.manifest = {"files": {}}
    assert hybrid.invoke("token") == []



@pytest.mark.parametrize("query,expected", [
    ("项目中是如何生成和验证token的？", "token"),
    ("项目中如何验证密码", "password"),
    ("项目中新闻存在哪个表中", "news"),
    ("项目在哪里使用了redis", "redis"),
    ("agent模块在哪个文件", "agent"),
])
def test_five_fixed_questions_find_source_in_synthetic_corpus(query, expected):
    """离线固定问题冒烟测试；不代表真实 Chroma/重排质量。"""
    docs = [
        chunk("token", "crud/users.py", "create_token", "# 生成和验证 token\ndef create_token(): pass"),
        chunk("password", "utils/security.py", "verify_password", "# 验证密码\ndef verify_password(): pass"),
        chunk("news", "models/news.py", "News", "# 新闻 表\nclass News: pass"),
        chunk("redis", "services/cache.py", "use_redis", "# 使用 redis 缓存\ndef use_redis(): pass"),
        chunk("agent", "agent/code_agent.py", "CodeAgent", "# agent 模块\nclass CodeAgent: pass"),
    ]
    hybrid, manager = retriever(docs)
    assert not manager.dense_ids  # 此测试只验证词法召回可找到证据。
    assert expected in [doc.id for doc in hybrid.invoke(query)]


def test_production_reranker_configuration_uses_requested_model(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    hybrid = HybridCodeRetriever(vector_manager=FakeManager([]))
    assert hybrid.reranker.model == "gte-rerank-v2"


@pytest.mark.parametrize("response", [[{"index": 99}], [{"index": 0}, {"index": 0}]])
def test_invalid_rerank_response_falls_back(response):
    docs = [chunk("a", "a.py", "alpha", "alpha"),
            chunk("b", "b.py", "beta", "beta")]
    class InvalidReranker:
        def rerank(self, documents, query, top_n):
            return response
    hybrid, _ = retriever(docs, dense_ids=("a", "b"), reranker=InvalidReranker())
    assert len(hybrid.invoke("anything")) == 2


def test_per_call_top_k_preserves_default_and_shared_retriever():
    docs = [chunk(str(i), f"file{i}.py", f"fn{i}", "token") for i in range(6)]
    hybrid, _ = retriever(docs, dense_ids=tuple(str(i) for i in range(6)))
    assert len(hybrid.invoke("token", top_k=5)) == 5
    assert len(hybrid.invoke("token")) == 3
    assert hybrid.reranker.calls[0][2] == 5
    with pytest.raises(ValueError):
        hybrid.invoke("token", top_k=0)
