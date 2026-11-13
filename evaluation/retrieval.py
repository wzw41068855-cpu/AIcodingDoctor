"""使用原题库真实运行混合检索，并对比子块与父上下文证据召回。"""

import argparse
import json
from pathlib import Path
import time

from rag.vector_store import VectorStoreManager
from services.code_search import CodeSearchService
from evaluation.prepare import prepare


def evaluate(source: Path, output: Path) -> dict:
    """复用一份真实索引，记录三十题的前后指标。

    Args:
        source: 原始 news_test 只读目录。
        output: 冻结评测产物目录。
    Returns:
        逐题证据、召回率、长度和耗时。
    """
    manifest = prepare(source, output)
    root = Path(manifest["repositories"]["news"])
    manager = VectorStoreManager(str(Path(manifest["root"]) / "retrieval_index"), "news_retrieval")
    service = CodeSearchService(root, vector_manager=manager)
    files = service.get_repository_summary()["files"]
    service.sync_changed_files(files)
    questions = json.loads((root / "evaluation/questions.json").read_text(encoding="utf-8"))
    rows = []
    for question in questions:
        started = time.monotonic()
        children = service.search_code(question["question"], 3)
        original = service.search_code
        service.search_code = lambda *args: children
        try:
            parents = service.search_context(question["question"], 3)
        finally:
            service.search_code = original
        gold = [symbol for item in question["gold"] for symbol in service.find_symbol(item["symbol"], item["file_path"])]
        def hits(candidates: list[dict]) -> bool:
            """匹配真实文件及金标准符号的定义行，不按回答文风评分。"""
            return all(any(c.get("relative_path") == g.relative_path and c.get("start_line", 0) <= g.start_line <= c.get("end_line", 0)
                           for c in candidates) for g in gold) and bool(gold)
        child_rows = [c.model_dump() for c in children]
        parent_rows = [p["parent"] for p in parents if p.get("parent")]
        rows.append({"id": question["id"], "query": question["question"], "gold": question["gold"],
            "child_hit": hits(child_rows), "parent_hit": hits(parent_rows),
            "child_chars": sum(len(c["content"]) for c in child_rows), "parent_chars": sum(len(p["content"]) for p in parent_rows),
            "seconds": time.monotonic() - started, "children": child_rows, "parents": parents})
        (output / "retrieval_progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"count": len(rows), "child_hit_at_3": sum(r["child_hit"] for r in rows) / len(rows),
              "parent_hit_at_3": sum(r["parent_hit"] for r in rows) / len(rows), "results": rows,
              "metric": "全部必需 gold 符号的定义行被当前源码区间覆盖"}
    (output / "retrieval.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source, args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "results"}))
