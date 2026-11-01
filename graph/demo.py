"""固定可信 demo 与明确标记的模拟模型/内存向量后端，不访问外部 API。"""

import json
from pathlib import Path
from types import SimpleNamespace

from graph.state import EditBatch, ReviewResult, TaskPlan
from rag.vector_store import VectorStoreManager
from services.code_search import CodeSearchService


class DemoStore:
    """仅供离线 demo 的内存 Chunk 存储，不声称执行真实 Embedding 或语义检索。

    Attributes:
        docs: 按 Chunk ID 保存的 Document，供真实 V1 增量协议调用。
    """

    def __init__(self):
        """初始化空 Chunk 字典，每次 demo 独立。"""
        self.docs = {}

    def get(self, *, ids=None, include=(), limit=None):
        """按 V1 存储协议读取 Chunk。

        Args:
            ids: 显式 ID；None 表示当前全部。
            include: 兼容 V1 参数；总是返回完整字段。
            limit: 可选条数上限。
        Returns:
            含 ids、documents、metadatas 的字典。
        """
        selected = [self.docs[key] for key in (self.docs if ids is None else ids) if key in self.docs]
        selected = selected if limit is None else selected[:limit]
        return {"ids": [d.id for d in selected], "documents": [d.page_content for d in selected],
                "metadatas": [d.metadata for d in selected]}

    def add_documents(self, documents, ids):
        """保存 V1 切分后的文档，不调用 Embedding。

        Args:
            documents: V1 已生成的文档列表。
            ids: 与文档一一对应的 Chunk ID。
        """
        for doc, key in zip(documents, ids, strict=True):
            self.docs[key] = doc.model_copy(update={"id": key})

    def delete(self, *, ids):
        """移除显式旧 Chunk，不访问磁盘索引。

        Args:
            ids: 要删除的 ID 列表；不存在的 ID 忽略。
        """
        for key in ids:
            self.docs.pop(key, None)


def demo_service(root: Path, storage: Path) -> CodeSearchService:
    """注入模拟向量存储，复用真实 V1 绑定、切分和增量同步协议。

    Args:
        root: 本轮可信 demo 副本。
        storage: 独立 manifest 的已有目录。
    Returns:
        初始同步成功的 V1 服务；检索为固定返回的模拟结果。
    """
    manager = VectorStoreManager.__new__(VectorStoreManager)
    manager.vector_store = DemoStore()
    manager.manifest = {"files": {}}
    manager.manifest_path = storage / "demo_manifest.json"
    retriever = SimpleNamespace(vector_manager=manager,
                                invoke=lambda query, top_k: list(manager.vector_store.docs.values())[:top_k])
    service = CodeSearchService(root, vector_manager=manager, retriever=retriever, max_file_bytes=65536)
    service.sync_changed_files(["calculator.py"])
    return service


def create_demo(root: Path) -> None:
    """在全新目录生成固定可信源码与验收测试，拒绝覆盖。

    Args:
        root: 不存在的演示副本目录。
    Raises:
        FileExistsError: 目录已存在。
    """
    root.mkdir(parents=True, exist_ok=False)
    (root / "tests").mkdir()
    (root / "calculator.py").write_text(
        '"""可信演示：加法实现故意含有减法缺陷。"""\n\n'
        'def add(a, b):\n    """返回两数之和，供修复图演示。"""\n    return a - b\n', encoding="utf-8")
    (root / "tests" / "test_calculator.py").write_text(
        '"""固定可见验收，修复器无权修改。"""\nfrom calculator import add\n\n'
        'def test_add():\n    """同时验证正数与负数输入。"""\n'
        '    assert add(2, 3) == 5\n    assert add(-2, 3) == 1\n', encoding="utf-8")


class DemoModel:
    """按固定策略输出结构化数据的模拟模型，绝不调用真实 LLM。

    Attributes:
        mode: success 一次成功；retry 首轮错误后成功；limit 每轮错误。
        rounds: 已生成的 Coder 补丁数量。
    """

    def __init__(self, mode: str = "success"):
        """校验 demo 策略并初始化计数。

        Args:
            mode: success、retry 或 limit。
        Raises:
            ValueError: 未知策略。
        """
        if mode not in {"success", "retry", "limit"}:
            raise ValueError("未知模拟策略")
        self.mode, self.rounds = mode, 0

    def with_structured_output(self, schema, **kwargs):
        """提供与官方模型相同的注入调用面，输出仍由真实 Pydantic 校验。

        Args:
            schema: TaskPlan、EditBatch 或 ReviewResult。
            **kwargs: 兼容官方 method 参数。
        Returns:
            带 invoke 的确定性模拟对象。
        """
        return SimpleNamespace(invoke=lambda messages, config=None: self._answer(schema, messages))

    def _answer(self, schema, messages):
        """根据真实最新文件和固定策略生成替换，仅适用于内置 calculator demo。"""
        payload = json.loads(messages[-1][1])
        if schema is TaskPlan:
            return TaskPlan(summary="修复加法", target_files=["calculator.py"], steps=["替换错误算式"],
                            acceptance_criteria=["正负数加法正确且可见测试通过"], test_strategy="运行固定 tests")
        if schema is ReviewResult:
            return ReviewResult(decision="passed", reason="实际 Diff 与可见测试符合加法需求")
        if schema is EditBatch:
            self.rounds += 1
            file = payload["files"][0]
            old = next(line.strip() for line in file["content"].splitlines() if line.strip().startswith("return "))
            wrong = self.mode == "limit" or self.mode == "retry" and self.rounds == 1
            new = f"return a + b + {self.rounds}" if wrong else "return a + b"
            return {"edits": [{"path": file["path"], "expected_hash": file["expected_hash"],
                               "old_text": old, "new_text": new}]}
        raise ValueError("不支持的模拟结构")
