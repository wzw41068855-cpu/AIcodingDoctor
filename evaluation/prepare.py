"""把用户提供的非 Git 目录复制为可复现快照，原目录只读。"""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from evaluation.tasks import TASKS, HIDDEN_TESTS
from graph.operations import digest
from workspace.manager import git


def snapshot(source: Path, target: Path, assistant: bool = False) -> dict:
    """复制代码并记录每个输入文件的内容哈希。

    Args:
        source: 只读源目录。
        target: 全新评测副本。
        assistant: 是否排除本项目测试与输出目录。
    Returns:
        源文件哈希清单。
    """
    ignored = {".git", ".idea", ".venv", "__pycache__", ".pytest_cache", "data", "logs"}
    if assistant:
        ignored |= {"tests", "docs", "evaluation"}
    def ignore(directory: str, names: list[str]) -> list[str]:
        """只复制非敏感代码和静态资源。"""
        return [n for n in names if n in ignored or n.startswith(".env") or n.endswith((".sqlite", ".db", ".pyc"))]
    shutil.copytree(source, target, ignore=ignore)
    hashes = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in target.rglob("*") if p.is_file()}
    git(target, "init")
    git(target, "add", ".")
    git(target, "-c", "user.name=V5 Evaluation", "-c", "user.email=eval@example.invalid", "commit", "-m", "frozen source snapshot")
    return hashes


def prepare(source: Path, output: Path) -> dict:
    """先冻结输入和验收，再允许实际模型运行。

    Args:
        source: news_test 目录。
        output: 评测报告目录。
    Returns:
        含 Git 基线、任务与输入哈希的冻结清单。
    """
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "frozen_tasks.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data["tasks_hash"] != digest(TASKS) or data["hidden_hash"] != digest(HIDDEN_TESTS):
            raise ValueError("冻结后不能更改评分标准；新评测必须使用新目录")
        return data
    root = Path(tempfile.mkdtemp(prefix="aicd-v5-news-evaluation-"))
    hashes = {"news": snapshot(source, root / "news"),
              "assistant": snapshot(Path(__file__).resolve().parents[1], root / "assistant", True)}
    repositories = {"news": str(root / "news"), "assistant": str(root / "assistant")}
    for task in TASKS:
        if task["mode"] != "repair" or not task.get("mutations"):
            continue
        target = root / task["id"]
        shutil.copytree(root / "news", target, ignore=shutil.ignore_patterns(".git"))
        for path, old, new in task["mutations"]:
            file = target / path
            text = file.read_text(encoding="utf-8")
            if text.count(old) != 1:
                raise ValueError("缺陷注入锚点不唯一：" + path)
            file.write_text(text.replace(old, new), encoding="utf-8")
        git(target, "init")
        git(target, "add", ".")
        git(target, "-c", "user.name=V5 Evaluation", "-c", "user.email=eval@example.invalid", "commit", "-m", "frozen seeded defect " + task["id"])
        repositories[task["id"]] = str(target)
    data = {"source": str(source), "root": str(root), "repositories": repositories,
            "source_hashes": hashes, "tasks": TASKS, "tasks_hash": digest(TASKS), "hidden_hash": digest(HIDDEN_TESTS),
            "commits": {key: git(Path(path), "rev-parse", "HEAD").strip() for key, path in repositories.items()}}
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
