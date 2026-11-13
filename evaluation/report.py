"""汇总实际记录、计数误差和原目录完整性，不调用远程模型。"""

import argparse
import hashlib
import json
from pathlib import Path
import statistics


def generate(output: Path) -> dict:
    """从不可变任务记录构造可审计摘要。

    Args:
        output: 真实评测产物目录。
    Returns:
        原目录完整性、任务分数及 Token 对照。
    """
    manifest = json.loads((output / "frozen_tasks.json").read_text(encoding="utf-8"))
    source = Path(manifest["source"])
    changed = [name for name, expected in manifest["source_hashes"]["news"].items()
               if not (source / name).is_file() or hashlib.sha256((source / name).read_bytes()).hexdigest() != expected]
    integrity = {"source": str(source), "checked_files": len(manifest["source_hashes"]["news"]),
                 "changed": changed, "git_created_in_original": (source / ".git").exists()}
    (output / "source_integrity.json").write_text(json.dumps(integrity, ensure_ascii=False, indent=2), encoding="utf-8")
    records = [json.loads((output / f"{task['id']}.json").read_text(encoding="utf-8")) for task in manifest["tasks"]]
    extras = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(output.glob("N?.json"))]
    for record in records + extras:
        metrics = record.get("context_metrics", [])
        path = Path(record["runtime"]) / "context_metrics.jsonl"
        if not metrics and path.exists():
            metrics = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            record["context_metrics"] = metrics
            (output / f"{record['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = [m for r in records + extras for m in r.get("context_metrics", []) if m.get("observed_input_tokens")]
    errors = [abs(m["estimated_input_tokens"] - m["observed_input_tokens"]) / m["observed_input_tokens"] for m in metrics]
    attempts = [json.loads(p.read_text(encoding="utf-8")) for p in (output / "attempts").glob("*.json")]
    short = next((r for r in extras if r["id"] == "N1"), {})
    long = next((r for r in extras if r["id"] == "N4"), {})
    summary = {"tasks": len(records), "latest_passed": sum(r["passed"] for r in records),
        "qa_passed": sum(r["passed"] for r in records if r["id"].startswith("Q")),
        "repair_passed": sum(r["passed"] for r in records if r["id"].startswith("R")),
        "safety_passed": sum(r["passed"] for r in records if r["id"].startswith("S")),
        "archived_attempts": len(attempts), "archived_failed": sum(not r["passed"] for r in attempts),
        "news_qa_passed": sum(r["passed"] for r in records + extras if r["id"] in {"Q2", "Q3", "Q4", "N1", "N2", "N3"}),
        "source_integrity": integrity,
        "model_tokens_latest_ten": sum((r.get("report") or {}).get("budget", {}).get("total_tokens", 0) for r in records),
        "model_calls_latest_ten": sum((r.get("report") or {}).get("budget", {}).get("model_calls", 0) for r in records),
        "context": {"measured_calls": len(metrics), "max_observed_input_tokens": max((m["observed_input_tokens"] for m in metrics), default=0),
                    "max_estimated_input_tokens": max((m["estimated_input_tokens"] for m in metrics), default=0),
                    "mean_absolute_percentage_error": statistics.mean(errors) if errors else None,
                    "underestimated_calls": sum(m["estimated_input_tokens"] < m["observed_input_tokens"] for m in metrics)},
        "history_comparison": {"short_passed": short.get("passed"), "long_passed": long.get("passed"),
            "short_total_tokens": (short.get("report") or {}).get("budget", {}).get("total_tokens"),
            "long_total_tokens": (long.get("report") or {}).get("budget", {}).get("total_tokens"),
            "max_compressions": max((m.get("compressed", 0) for m in long.get("context_metrics", [])), default=0)},
        "limitations": ["开发调试后的固定集合回放，不是独立泛化测试", "四项修复为显式人工注入缺陷", "未采集Embedding/Rerank计费Token，不估算货币费用", "浏览器URL识别失败，人工UI验收未完成"]}
    (output / "audit_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=True, indent=2))
