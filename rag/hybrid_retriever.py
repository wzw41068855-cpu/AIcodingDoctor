"""在已提交的 Chroma Chunk 上组合 Dense、BM25、RRF 和重排。"""

import os
import re
from threading import RLock
from typing import Any

import dashscope
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.document_compressors.dashscope_rerank import DashScopeRerank
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableLambda
from pydantic import PrivateAttr

import config
from utils.logger import get_logger
from utils.operation_timing import timed, timed_operation

logger = get_logger("hybrid_retriever")
_FUSION_ID = "_hybrid_chunk_id"
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[\u3400-\u9fff]+|\d+")
_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.(?:py|md)\b", re.IGNORECASE)

#自定义 HybridCodeRetriever，在内部组合 Dense、BM25 和 Rerank。
'''
 hybrid_retriever.py 的工作：只用索引清单中已提交的 Chroma 代码片段（Chunk），组合两种搜索，再融合和重排结果。
实际流程是：
1.
从清单读取有效的 Chunk ID，加载源码，并在内存中建立 BM25 索引；清单变化时才重建快照。
2.
Dense 搜索按向量语义找相近代码；BM25 按词项找匹配代码，分词时会照顾文件路径、snake_case、camelCase 和中文。
3.
RRF 根据两路结果的名次融合排序，用 Chunk ID 识别并合并重复结果。
4.
把融合后的候选交给 DashScope 重排，返回最相关的结果；重排失败时回退到 RRF 排序。
当前配置是两路各取最多 12 个候选、融合后取最多 12 个重排、最终返回最多 3 个，见 config.py。这些是上限，实际结果可能更少。整个检索过程不会修改索引，返回的仍是原始源码及其来源信息。
'''


def code_tokens(text: str) -> list[str]:
    """保留完整标识符，同时拆出 snake/camel 子词和中文双字词。"""
    tokens = [path.replace("\\", "/").lower() for path in _PATH_RE.findall(text)]
    for match in _WORD_RE.finditer(text):
        word = match.group()
        tokens.append(word.lower())
        if "_" in word or re.search(r"[a-z0-9][A-Z]", word):
            separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word)
            tokens.extend(part.lower() for part in separated.split("_") for part in part.split() if part)
        if "\u3400" <= word[0] <= "\u9fff" and len(word) > 2:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


def _searchable_text(doc: Document) -> str:
    """BM25 的临时文本可包含来源字段；Tool 返回的源码仍保持原样。"""
    metadata = doc.metadata
    fields = ("relative_path", "module_path", "qualified_name", "symbol_name", "symbol_names")
    return "\n".join([*(str(metadata.get(key) or "") for key in fields), doc.page_content])


class HybridCodeRetriever(BaseRetriever):
    """
    检索器继承BaseRetriever，实现：
    1. 从向量数据库获取密集型检索结果；
    2. 从 manifest 获取稀疏型检索结果；
    3. 使用 RRF 融合两种检索结果；
    4. 使用阿里云 Rerank 对结果进行重排。

    只读检索器；BM25 是从 manifest 派生的进程内快照。"""

    vector_manager: Any
    reranker: Any = None
    _lock: RLock = PrivateAttr(default_factory=RLock)
    _fingerprint: tuple | None = PrivateAttr(default=None)
    _bm25: BM25Retriever | None = PrivateAttr(default=None)
    _originals: dict[str, Document] = PrivateAttr(default_factory=dict)
    _term_sets: dict[str, frozenset[str]] = PrivateAttr(default_factory=dict)
    _fuser: EnsembleRetriever = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        # 当前 community 版本的便捷构造会覆盖 model；显式传入 SDK client。
        if self.reranker is None:
            key = os.getenv("DASHSCOPE_API_KEY")
            if not key:
                raise ValueError("缺少 DASHSCOPE_API_KEY 环境变量")
            self.reranker = DashScopeRerank(#创建阿里云 Rerank 对象。
                client=dashscope.TextReRank,#执行请求的 DashScope SDK 客户端。
                model=config.RERANK_MODEL,#具体重排模型。
                top_n=config.HYBRID_TOP_K,#：最终保留多少个结果。
                api_key=key,#：API 密钥。
            )
        self._fuser = EnsembleRetriever(#EnsembleRetriever 使用 RRF 融合两路搜索的排名。
            retrievers=[RunnableLambda(self._dense_search), RunnableLambda(self._lexical_search)],
            #RunnableLambda 把普通 Python 函数包装成 LangChain Runnable。
            weights=[0.5, 0.5],
            c=config.HYBRID_RRF_C,
            id_key=_FUSION_ID,#id_key 告诉融合器根据 metadata 中的哪个标识识别相同 Document
        )

    def _manifest_fingerprint(self) -> tuple:
        files = self.vector_manager.manifest["files"]
        return tuple(sorted(
            (path, info["hash"], info.get("signature"), tuple(info["ids"]))
            for path, info in files.items()
        ))

    def _ensure_snapshot(self) -> None:
        with self._lock:
            fingerprint = self._manifest_fingerprint()
            if fingerprint == self._fingerprint:
                return

            ids = [chunk_id for _, _, _, file_ids in fingerprint for chunk_id in file_ids]
            loaded: dict[str, Document] = {}
            # 只读 manifest 已提交的 ID；避开同步中途写入的临时向量。
            for offset in range(0, len(ids), 100):
                with timed_operation("数据库：读取 Chroma 检索快照批次"):
                    rows = self.vector_manager.vector_store.get(
                        ids=ids[offset:offset + 100], include=["documents", "metadatas"]
                    )
                for chunk_id, content, metadata in zip(
                    rows["ids"], rows["documents"], rows["metadatas"], strict=True
                ):
                    if isinstance(content, str):
                        loaded[chunk_id] = Document(
                            id=chunk_id, page_content=content, metadata=metadata or {}
                        )

            originals = {chunk_id: loaded[chunk_id] for chunk_id in ids if chunk_id in loaded}
            lexical_docs = [
                Document(id=chunk_id, page_content=_searchable_text(doc), metadata={_FUSION_ID: chunk_id})
                for chunk_id, doc in originals.items()
            ]
            term_sets = {
                doc.id: frozenset(code_tokens(doc.page_content)) for doc in lexical_docs
            }
            lexical_docs = [doc for doc in lexical_docs if term_sets[doc.id]]
            # 空 Collection 合法；BM25Retriever 不能用空语料构建。
            with timed_operation("检索：构建 BM25 快照"):
                bm25 = (BM25Retriever.from_documents(
                    lexical_docs, preprocess_func=code_tokens, k=config.HYBRID_BM25_K
                ) if lexical_docs else None)
            self._originals = originals
            self._term_sets = term_sets
            self._bm25 = bm25
            self._fingerprint = fingerprint

    @timed("检索：Dense 召回")
    def _dense_search(self, query: str) -> list[Document]:
        self._ensure_snapshot()
        with self._lock:
            valid_ids = frozenset(self._originals)
        if not valid_ids:
            return []
        docs = self.vector_manager.similarity_search(query, k=config.HYBRID_DENSE_K)
        return [
            doc.model_copy(update={"metadata": {**doc.metadata, _FUSION_ID: doc.id}})
            for doc in docs if doc.id in valid_ids
        ]

    @timed("检索：BM25 召回")
    def _lexical_search(self, query: str) -> list[Document]:
        self._ensure_snapshot()
        with self._lock:
            bm25, originals, term_sets = self._bm25, self._originals, self._term_sets
        query_terms = set(code_tokens(query))
        if bm25 is None or not query_terms:
            return []
        matches = bm25.invoke(query)
        # 官方 BM25 会补齐零分结果；无任何词项相交的候选不参与 RRF。
        return [
            originals[doc.id].model_copy(update={
                "metadata": {**originals[doc.id].metadata, _FUSION_ID: doc.id}
            })
            for doc in matches
            if doc.id in originals and query_terms & term_sets[doc.id]
        ]

    def _get_relevant_documents(self, query: str, *, run_manager, top_k: int | None = None) -> list[Document]:
        top_k = config.HYBRID_TOP_K if top_k is None else top_k
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        if not query.strip():
            raise ValueError("查询文本不能为空")
        self._ensure_snapshot()
        if not self._originals:
            return []
        with timed_operation("检索：双路召回与 RRF 融合"):
            fused = self._fuser.invoke(query, config={"callbacks": run_manager.get_child()})
        # 去掉仅供融合使用的键；直接保留本次融合得到的源码副本，
        # 即使同步恰好更新了内存快照，也不重新查表而误取另一个版本。
        candidates = [
            doc.model_copy(update={"metadata": {
                key: value for key, value in doc.metadata.items() if key != _FUSION_ID
            }})
            for doc in fused[:config.HYBRID_RRF_K]
        ]
        if not candidates:
            return []
        fallback = candidates[:top_k]
        try:
            with timed_operation("检索：DashScope Rerank 调用"):
                ranked = self.reranker.rerank(candidates, query, top_n=top_k)
            indexes = [item["index"] for item in ranked]
            if (len(indexes) != min(top_k, len(candidates))
                    or len(set(indexes)) != len(indexes)
                    or any(type(index) is not int or not 0 <= index < len(candidates)
                           for index in indexes)):
                raise ValueError("重排结果包含无效或缺失的候选序号")
            return [candidates[index] for index in indexes]
        except Exception as exc:
            logger.warning("Rerank 失败，回退 RRF 结果：%s", exc)
            return fallback
