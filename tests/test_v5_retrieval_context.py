"""冻结的十二类父子检索场景与源码引用校验。"""

from pathlib import Path
import pytest
from langchain_core.documents import Document

from rag.splitter.code_splitter import CodeSplitter
from services.code_search import CodeSearchService, CodeResult
from services.code_context import source_version
from services.citations import validate_citations


CASES = [
    ("function", "def f():\n    return 1\n", 1),
    ("method", "class A:\n    flag = True\n    def f(self):\n        return self.flag\n", 3),
    ("class_attribute", "class A:\n    setting = 3\n", 2),
    ("decorated", "@staticmethod\ndef f():\n    return 1\n", 2),
    ("async", "async def f():\n    return 1\n", 1),
    ("nested", "def outer():\n    def f():\n        return 1\n    return f()\n", 2),
    ("duplicate", "class A:\n    def f(self):\n        return 1\nclass B:\n    def f(self):\n        return 2\n", 5),
    ("unicode", "def f():\n    return '中文内容'\n", 2),
    ("syntax_error", "def broken(:\n    pass\n", 1),
    ("long_parent", "class A:\n" + "    value = 1\n" * 150 + "    def f(self):\n        return 1\n", 152),
    ("changed", "def f():\n    return 1\n", 1),
    ("deleted", "def f():\n    return 1\n", 1),
]


@pytest.mark.parametrize("name,text,line", CASES)
def test_frozen_parent_cases(tmp_path: Path, name: str, text: str, line: int) -> None:
    """父范围、过期与降级行为有固定期望。"""
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "module.py"
    path.write_text(text, encoding="utf-8")
    service = CodeSearchService(root)
    child = CodeResult(relative_path="module.py", content=text.splitlines()[line-1],
                       start_line=line, end_line=line, symbol_name="f", source_version=source_version(text))
    service.search_code = lambda *args: [child]
    if name == "changed":
        path.write_text(text.replace("return 1", "return 2"), encoding="utf-8")
    if name == "deleted":
        path.unlink()
    rows = service.search_context("f")
    assert rows
    if name == "deleted":
        assert rows[0]["deleted"] and rows[0]["parent"] is None
        return
    row = rows[0]
    parent = row["parent"]
    current = path.read_text(encoding="utf-8").splitlines(True)
    assert parent["content"] == "".join(current[parent["start_line"]-1:parent["end_line"]])
    assert parent["start_line"] <= line <= parent["end_line"]
    assert len(parent["content"]) <= 6000
    if name == "method":
        assert "flag = True" in parent["content"]
    if name == "changed":
        assert row["stale"] and "return 2" in parent["content"]


def test_chunk_parent_metadata_and_references(tmp_path: Path) -> None:
    """子块有父标识，静态调用返回实际行号。"""
    text = "class A:\n    def f(self):\n        return 1\n\ndef call():\n    return A().f()\n"
    doc = Document(page_content=text, metadata={"relative_path": "module.py", "file_type": ".py"})
    chunks = CodeSplitter(chunk_size=60, chunk_overlap=0).split([doc])
    assert all(c.metadata["parent_id"] and c.metadata["source_version"] for c in chunks)
    (tmp_path / "module.py").write_text(text, encoding="utf-8")
    service = CodeSearchService(tmp_path)
    assert service.find_references("f")[0].start_line == 6
    fact = service.get_code_context("module.py").model_dump()
    assert validate_citations("调用位于 module.py:6", [fact], tmp_path)
    with pytest.raises(ValueError):
        validate_citations("伪造 module.py:99", [fact], tmp_path)
    with pytest.raises(ValueError):
        service.get_code_context("../escape.py")
    (tmp_path / "module.py").write_text(text + "# changed\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_citations("过期 module.py:6", [fact], tmp_path)
