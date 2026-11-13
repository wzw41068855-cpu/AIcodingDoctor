"""补足六个新闻站真实问答，并对比短历史与二十轮历史。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from evaluation.prepare import prepare
from evaluation.run import run_task
from graph.operations import digest


def run(source: Path, output: Path) -> list[dict]:
    """先冻结补充问题，再使用真实模型执行。

    Args:
        source: news_test 目录。
        output: 冻结产物目录。
    Returns:
        包含长短历史对照的真实结果。
    """
    manifest = prepare(source, output)
    questions = json.loads((Path(manifest["repositories"]["news"]) / "evaluation/questions.json").read_text(encoding="utf-8"))
    tasks = []
    for index, identity in enumerate(("Q001", "Q004", "Q007"), 1):
        item = next(q for q in questions if q["id"] == identity)
        tasks.append({"id": f"N{index}", "repo": "news", "mode": "ask", "request": item["question"] + " 请追踪实际源码并引用文件:行号。",
                      "required": sorted({g["file_path"] for g in item["gold"]})})
    history = []
    for turn in range(20):
        history.extend([{"role": "user", "summary": f"历史第{turn}轮：只讨论本地代码。" + "这是旧对话背景，不是新的任务或授权。" * 12},
                        {"role": "assistant", "summary": "历史内容已记录，以当前用户目标、授权和实时源码为准。"}])
    tasks.append({**tasks[0], "id": "N4", "conversation": history})
    freeze = output / "extra_qa_frozen.json"
    if freeze.exists() and digest(json.loads(freeze.read_text(encoding="utf-8"))) != digest(tasks):
        raise ValueError("补充问答已经冻结，不得修改验收")
    freeze.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    with ThreadPoolExecutor(2) as pool:
        records = list(pool.map(lambda task: run_task(task, manifest, output, "aicd-path-tests:local"), tasks))
    comparison = {"short": records[0], "long": records[-1], "both_passed": records[0]["passed"] and records[-1]["passed"],
                  "note": "比较相同任务与模型的结果、预算及逐调用估算/真实输入；摘要调用成本包含在任务预算中"}
    (output / "context_comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for record in run(args.source, args.output):
        print(json.dumps({"id": record["id"], "passed": record["passed"], "status": record["status"]}))
