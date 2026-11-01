"""V1 离线服务与索引一致性回归；仅访问临时目录和内存向量。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from rag.vector_store import VectorStoreManager
from services.code_search import CodeSearchService


class MemoryStore:
    def __init__(self):
        self.docs = {}
        self.writes = 0
        self.fail_add = False
        self.fail_delete = False

    def get(self, *, ids=None, include=(), limit=None):
        selected = [self.docs[key] for key in (self.docs if ids is None else ids) if key in self.docs]
        if limit is not None:
            selected = selected[:limit]
        return {"ids": [d.id for d in selected], "documents": [d.page_content for d in selected],
                "metadatas": [d.metadata for d in selected]}

    def add_documents(self, documents, ids):
        self.writes += len(ids)
        for doc, key in zip(documents, ids):
            self.docs[key] = doc.model_copy(update={"id": key})
            if self.fail_add:
                raise RuntimeError("模拟部分向量写入失败")

    def delete(self, *, ids):
        if self.fail_delete:
            raise RuntimeError("模拟清理失败")
        for key in ids:
            self.docs.pop(key, None)

    def similarity_search(self, **kwargs):
        return list(self.docs.values())[:kwargs["k"]]


@pytest.fixture
def setup_service(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    manager = VectorStoreManager.__new__(VectorStoreManager)
    manager.vector_store = MemoryStore()
    manager.manifest = {"files": {}}
    manager.manifest_path = tmp_path / "isolated_manifest.json"
    service = CodeSearchService(root, vector_manager=manager)
    return root, manager, service


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_search_format_and_unknown_metadata(tmp_path):
    doc = Document(page_content="def f(): pass", metadata={
        "relative_path": "a.py", "source": str(tmp_path / "a.py"),
        "symbol_name": "f", "start_line": 2, "end_line": 2})
    calls = []
    def invoke(query, **kwargs):
        calls.append((query, kwargs))
        return [doc, Document(page_content="unknown")]
    service = CodeSearchService(tmp_path, retriever=SimpleNamespace(invoke=invoke))
    results = service.search_code("function", top_k=2)
    assert calls == [("function", {"top_k": 2})]
    assert results[0].relative_path == "a.py"
    assert results[0].start_line == results[0].end_line == 2
    assert results[0].symbol_name == "f" and results[0].score is None
    assert results[1].relative_path is None and results[1].start_line is None
    assert len(service.search_code("function", top_k=1)) == 1
    for query, k in [(" ", 1), ("f", 0), ("f", True)]:
        with pytest.raises(ValueError):
            service.search_code(query, k)


def test_search_rejects_unsafe_metadata(tmp_path):
    retriever = SimpleNamespace(invoke=lambda *a, **kw: [
        Document(page_content="private", metadata={"relative_path": "../other.py"})])
    service = CodeSearchService(tmp_path, retriever=retriever)
    with pytest.raises(ValueError):
        service.search_code("f")
    with pytest.raises(RuntimeError):
        CodeSearchService(tmp_path).search_code("f")


def test_context_current_physical_lines_and_ranges(tmp_path):
    path = write(tmp_path, "a.py", 'value = "中文\u2028内容"\n\nlast = 1\n')
    service = CodeSearchService(tmp_path)
    assert service.get_code_context("a.py").end_line == 3
    result = service.get_code_context(str(path), 2, 3)
    assert result.content == "\nlast = 1\n" and result.start_line == 2
    path.write_text("new = 2", encoding="utf-8")
    assert service.get_code_context("a.py").content == "new = 2"
    for first, last in [(0, None), (2, 1), (1, 2), (-1, 1), (True, 1), (1.5, 2)]:
        with pytest.raises(ValueError):
            service.get_code_context("a.py", first, last)
    with pytest.raises(FileNotFoundError):
        service.get_code_context("missing.py")
    write(tmp_path, "empty.md", "")
    assert service.get_code_context("empty.md").start_line is None
    with pytest.raises(ValueError):
        service.get_code_context("empty.md", 1)


@pytest.mark.parametrize("path", ["../outside.py", ".env", ".git/config.py", "secrets.py", "data.bin"])
def test_sensitive_and_escape_paths(tmp_path, path):
    with pytest.raises(ValueError):
        CodeSearchService(tmp_path).get_code_context(path)


def test_binary_and_large_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "binary.py").write_bytes(b"x\x00y")
    write(root, "large.md", "x" * 21)
    service = CodeSearchService(root, max_file_bytes=20)
    for file in ["binary.py", "large.md"]:
        with pytest.raises(ValueError):
            service.get_code_context(file)


def test_real_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    service = CodeSearchService(root)
    external = write(tmp_path, "outside.py", "external = 1")
    try:
        (root / "link.py").symlink_to(external)
    except OSError:
        pytest.skip("系统未授予创建符号链接权限")
    with pytest.raises(ValueError):
        service.get_code_context("link.py")


def test_symbols_reuse_ast_full_definitions(tmp_path):
    write(tmp_path, "a.py", "@decorate\nclass A:\n    @staticmethod\n    async def same():\n        def nested(): pass\n        return 1\n\ndef same(): pass\n")
    write(tmp_path, "b.py", "class B:\n    def same(self): pass\n")
    service = CodeSearchService(tmp_path)
    same = service.find_symbol("same")
    assert {r.qualified_name for r in same} == {"A.same", "same", "B.same"}
    method = service.find_symbol("A.same", "a.py")[0]
    assert method.start_line == 3 and method.end_line == 6
    assert method.symbol_type == "method" and method.content.startswith("    @staticmethod")
    cls = service.find_symbol("A", "a.py")[0]
    assert cls.start_line == 1 and cls.end_line == 6
    assert "return 1" in cls.content
    assert service.find_symbol("A.same.nested")[0].symbol_type == "nested_function"
    assert service.find_symbol("missing") == []
    with pytest.raises(ValueError):
        service.find_symbol("")
    write(tmp_path, "broken.py", "def (")
    with pytest.raises(SyntaxError):
        service.find_symbol("same")


def test_summary_bounded_and_ignored(tmp_path):
    for path in ["a.py", "sub/b.md", ".git/x.py", ".env.py", "secrets.py"]:
        write(tmp_path, path, "x")
    service = CodeSearchService(tmp_path)
    assert service.get_repository_summary() == {"files": ["a.py", "sub/b.md"], "truncated": False}
    assert service.get_repository_summary(1) == {"files": ["a.py"], "truncated": True}
    for limit in [0, 1001, True]:
        with pytest.raises(ValueError):
            service.get_repository_summary(limit)


def test_incremental_add_modify_delete_repeat_and_bm25(setup_service):
    from rag.hybrid_retriever import HybridCodeRetriever
    root, manager, service = setup_service
    class Reranker:
        def rerank(self, documents, query, top_n):
            return [{"index": i} for i in range(min(top_n, len(documents)))]
    service.retriever = HybridCodeRetriever(vector_manager=manager, reranker=Reranker())
    write(root, "a.py", "def token(): return 'old'\n")
    write(root, "b.md", "token documentation")
    assert service.sync_changed_files(["a.py", "b.md"])["added"] == 2
    assert any("old" in r.content for r in service.search_code("token"))
    writes = manager.vector_store.writes
    assert service.sync_changed_files(["a.py"])["skipped"] == 1
    assert manager.vector_store.writes == writes
    write(root, "a.py", "def token(): return 'new'\n")
    assert service.sync_changed_files(["a.py"])["updated"] == 1
    results = service.search_code("token", 8)
    assert any("new" in r.content for r in results) and all("old" not in r.content for r in results)
    assert "b.md" in manager.manifest["files"]
    (root / "a.py").unlink()
    assert service.sync_changed_files(["a.py"])["deleted"] == 1
    assert all(r.relative_path != "a.py" for r in service.search_code("token"))
    (root / "b.md").unlink()
    assert service.sync_changed_files(["b.md"])["deleted"] == 1
    assert service.search_code("token") == []
    assert manager.vector_store.docs == {} and manager.manifest["files"] == {}
    assert manager._load_manifest() == manager.manifest
    assert service.sync_changed_files([]) == dict(added=0, updated=0, skipped=0, deleted=0)


@pytest.mark.parametrize("replacement", ["", " \n", "# 只有注释\n"])
def test_empty_or_comment_update_clears_old_chunks(setup_service, replacement):
    root, manager, service = setup_service
    write(root, "a.py", "x = 1\n")
    service.sync_changed_files(["a.py"])
    write(root, "a.py", replacement)
    assert service.sync_changed_files(["a.py"])["updated"] == 1
    assert manager.manifest["files"]["a.py"]["ids"] == []
    assert manager.vector_store.docs == {}
    assert service.sync_changed_files(["a.py"])["skipped"] == 1


def test_partial_load_failure_aborts_deletions(setup_service):
    root, manager, service = setup_service
    write(root, "a.py", "x = 1")
    write(root, "b.md", "old")
    service.sync_changed_files(["a.py", "b.md"])
    before = deepcopy(manager.manifest)
    (root / "a.py").write_bytes(b"\x00")
    (root / "b.md").unlink()
    with pytest.raises(RuntimeError, match="加载失败"):
        service.sync_changed_files(["a.py", "b.md"])
    assert manager.manifest == before and manager._load_manifest() == before
    assert len(service.loader.failed_files) == 1
    write(root, "a.py", "x = 2")
    service.sync_changed_files(["a.py"])
    assert not service.loader.failed_files and "b.md" in manager.manifest["files"]


@pytest.mark.parametrize("failure", ["embedding", "manifest"])
def test_failure_preserves_old_manifest_and_vectors(setup_service, monkeypatch, failure):
    root, manager, service = setup_service
    write(root, "a.py", "old = 1")
    service.sync_changed_files(["a.py"])
    before, docs = deepcopy(manager.manifest), deepcopy(manager.vector_store.docs)
    write(root, "a.py", "new = 2")
    if failure == "embedding":
        manager.vector_store.fail_add = True
    else:
        monkeypatch.setattr("rag.vector_store.os.replace", lambda *args: (_ for _ in ()).throw(OSError("模拟原子替换失败")))
    with pytest.raises((RuntimeError, OSError)):
        service.sync_changed_files(["a.py"])
    assert manager.manifest == before and manager._load_manifest() == before
    assert manager.vector_store.docs == docs
    assert not manager.manifest_path.with_suffix(".json.tmp").exists()
    if failure == "embedding":
        manager.vector_store.fail_add = False
        assert service.sync_changed_files(["a.py"])["updated"] == 1


def test_post_commit_cleanup_failure_keeps_committed_snapshot(setup_service, monkeypatch):
    root, manager, service = setup_service
    write(root, "a.py", "old = 1")
    service.sync_changed_files(["a.py"])
    original = manager._cleanup_orphans
    count = 0
    def cleanup(manifest):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("模拟提交后清理失败")
        original(manifest)
    monkeypatch.setattr(manager, "_cleanup_orphans", cleanup)
    write(root, "a.py", "new = 2")
    with pytest.raises(OSError):
        service.sync_changed_files(["a.py"])
    assert manager._load_manifest() == manager.manifest
    assert len(manager.vector_store.docs) == 2
    monkeypatch.setattr(manager, "_cleanup_orphans", original)
    assert service.sync_changed_files(["a.py"])["skipped"] == 1
    assert len(manager.vector_store.docs) == 1


def test_repository_binding_and_legacy_validation(setup_service, tmp_path):
    root, manager, service = setup_service
    write(root, "a.py", "x = 1")
    service.sync_changed_files(["a.py"])
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="其他仓库"):
        CodeSearchService(other, vector_manager=manager)
    manager.manifest.pop("repository_root")
    CodeSearchService(root, vector_manager=manager)
    with pytest.raises(ValueError):
        CodeSearchService(other, vector_manager=manager)


def test_manager_rejects_empty_full_sync_and_bad_source(setup_service):
    from rag.splitter.code_splitter import CodeSplitter
    root, manager, service = setup_service
    with pytest.raises(ValueError):
        manager.sync_documents([], CodeSplitter(), remove_missing=True)
    doc = Document(page_content="x = 1", metadata={"relative_path": "a.py", "source": str(root.parent / "a.py")})
    with pytest.raises(ValueError):
        manager.sync_documents([doc], CodeSplitter(), repository_root=str(root))
    with pytest.raises(RuntimeError):
        CodeSearchService(root).sync_changed_files(["a.py"])
    with pytest.raises(ValueError):
        service.sync_changed_files("a.py")



def test_legacy_manager_call_infers_root_and_rejects_other_repo(setup_service, tmp_path):
    from rag.splitter.code_splitter import CodeSplitter
    from rag.loader import Loader
    root, manager, service = setup_service
    write(root, "sub/a.py", "x = 1")
    assert manager.sync_documents(Loader(str(root)).load(), CodeSplitter())["added"] == 1
    assert manager.manifest["repository_root"] == str(root.resolve())
    other = tmp_path / "other"
    write(other, "sub/a.py", "x = 2")
    before = deepcopy(manager.manifest)
    with pytest.raises(ValueError):
        manager.sync_documents(Loader(str(other)).load(), CodeSplitter())
    assert manager.manifest == before


def test_failed_rollback_is_recovered_on_next_sync(setup_service):
    root, manager, service = setup_service
    write(root, "a.py", "old = 1")
    service.sync_changed_files(["a.py"])
    before = deepcopy(manager.manifest)
    write(root, "a.py", "new = 2")
    manager.vector_store.fail_add = manager.vector_store.fail_delete = True
    with pytest.raises(RuntimeError, match="写入失败"):
        service.sync_changed_files(["a.py"])
    assert manager.manifest == before and manager._load_manifest() == before
    assert len(manager.vector_store.docs) == 2
    manager.vector_store.fail_add = manager.vector_store.fail_delete = False
    assert service.sync_changed_files(["a.py"])["updated"] == 1
    assert len(manager.vector_store.docs) == 1


def test_legacy_manifest_missing_chunk_is_rejected(setup_service):
    root, manager, service = setup_service
    write(root, "a.py", "x = 1")
    service.sync_changed_files(["a.py"])
    manager.manifest.pop("repository_root")
    manager.vector_store.docs.clear()
    with pytest.raises(ValueError, match="缺少 Chunk"):
        CodeSearchService(root, vector_manager=manager)


def test_symlink_resolution_boundary_without_os_privilege(tmp_path, monkeypatch):
    write(tmp_path, "alias.py", "x = 1")
    service = CodeSearchService(tmp_path)
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == tmp_path / "alias.py":
            return tmp_path.parent / "outside.py"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="符号链接"):
        service.get_code_context("alias.py")
