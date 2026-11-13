"""四类任务记忆与按角色预算装配上下文，复用 LangChain Token 估算。"""

import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

import config
from graph.test_runner import redact_data
from memory.artifacts import Artifacts


def token_count(text: str) -> int:
    """使用官方估算器，计数方式在报告中明确为估算。

    Args:
        text: 待装配文本。
    Returns:
        包含消息开销的估算 Token。
    """
    return int(count_tokens_approximately([HumanMessage(content=text)], chars_per_token=config.CONTEXT_CHARS_PER_TOKEN))


class ContextBuilder:
    """持久对话、事实、摘要和工具引用；不持久保存模型内部推理。"""

    def __init__(self, directory: Path, counter: Callable[[str], int] = token_count,
                 input_tokens: int = config.CONTEXT_INPUT_TOKENS) -> None:
        """读取任务独立记忆，恢复不丢失引用。

        Args:
            directory: 任务目录。
            counter: 可替换的 Token 计数器。
            input_tokens: 输入硬上限。
        """
        if input_tokens <= 0 or input_tokens + config.CONTEXT_OUTPUT_RESERVE + config.CONTEXT_SAFETY_TOKENS > config.CONTEXT_WINDOW_TOKENS:
            raise ValueError("输入预算与输出预留超过模型窗口")
        self.path, self.counter, self.input_tokens = directory / "memory.sqlite", counter, input_tokens
        self.artifacts = Artifacts(directory)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute("INSERT OR IGNORE INTO memory VALUES (1,?)", (json.dumps({
                "conversation": [], "facts": [], "summary": "", "tools": [], "compressed": 0}),))

    def load(self) -> dict:
        """读取四类记忆。

        Returns:
            任务独立状态。
        """
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute("SELECT data FROM memory WHERE id=1").fetchone()[0])

    def save(self, data: dict) -> None:
        """更新记忆。

        Args:
            data: 已脱敏状态。
        """
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE memory SET data=? WHERE id=1", (json.dumps(redact_data(data), ensure_ascii=False),))

    def observe(self, tool: str, result: Any) -> Any:
        """提取位置事实并卸载大型结果，不把摘要升级为授权。

        Args:
            tool: 已执行工具。
            result: 真实工具返回。
        Returns:
            有界内联结果或产物引用。
        """
        data = self.load()
        def extract(value: Any) -> None:
            """只提取结构字段，不从源码中的指令提取权限。"""
            if isinstance(value, list):
                for item in value:
                    extract(item)
            if isinstance(value, dict):
                if value.get("relative_path") and value.get("start_line"):
                    fact = {k: value.get(k) for k in ("relative_path", "start_line", "end_line", "source_version")}
                    if fact not in data["facts"]:
                        data["facts"].append(fact)
                for key in ("parent",):
                    if isinstance(value.get(key), dict):
                        extract(value[key])
        extract(result)
        raw = json.dumps(redact_data(result), ensure_ascii=False)
        if len(raw) > config.CONTEXT_TOOL_INLINE_CHARS:
            key = self.artifacts.save(result)
            data["tools"].append({"tool": tool, "artifact_id": key})
            inline = {k: v for k, v in result.items() if k != "content"} if isinstance(result, dict) else {}
            # 非内容字段也可能嵌套大型对象，不能原样带回模型。
            inline = {k: (v[:config.CONTEXT_TOOL_INLINE_CHARS // 2] if isinstance(v, str) else v)
                      for k, v in inline.items() if isinstance(v, (str, int, float, bool, type(None)))}
            if isinstance(result, dict) and isinstance(result.get("command"), list):
                inline["command"] = result["command"]
            inline.update(artifact_id=key, preview=raw[:config.CONTEXT_TOOL_INLINE_CHARS // 2], truncated=True)
            if isinstance(result, dict) and isinstance(result.get("content"), str):
                inline["content"] = result["content"][:config.CONTEXT_TOOL_INLINE_CHARS // 2]
                inline.pop("preview", None)
            result = inline
        self.save(data)
        return result

    def build(self, role: str, payload: dict, summarizer: Callable[[str], str] | None = None,
              char_limit: int = config.AGENT_CONTEXT_CHARS, reserved_tokens: int = 0) -> dict:
        """保留目标与硬事实，滚动压缩旧对话和工具观察。

        Args:
            role: 可信角色。
            payload: 任务状态装配输入。
            summarizer: 可选的有预算 LLM 摘要函数。
            char_limit: 第二道字符保护。
            reserved_tokens: 同一次调用的 System Prompt 估算用量。
        Returns:
            在指定计数器下满足预算的上下文。
        Raises:
            ValueError: 必需约束本身超过预算，不能静默丢失。
        """
        data = self.load()
        budget = int(self.input_tokens * config.CONTEXT_ROLE_FRACTIONS.get(role, 1.0)) - reserved_tokens
        result = {k: v for k, v in payload.items() if k not in {"history", "evidence"}}
        histories = list(payload.get("history", []))
        evidence = list(payload.get("evidence", []))
        data["conversation"] = evidence
        if len(evidence) > config.CONTEXT_SUMMARIZE_AFTER:
            old = evidence[:-config.CONTEXT_RECENT_RESULTS]
            key = self.artifacts.save(old)
            if key != data.get("summary_source"):
                text = json.dumps(old, ensure_ascii=False)
                try:
                    summary = summarizer(text) if summarizer else ""
                except Exception:
                    summary = ""
                data["summary"] = summary[:config.CONTEXT_SUMMARY_TOKENS * 2] or f"旧对话已卸载，可读取 {key}；关键源码位置独立保留。"
                data.update(summary_source=key, compressed=data["compressed"] + 1)
            evidence = evidence[-config.CONTEXT_RECENT_RESULTS:]
            data["conversation"] = evidence
        def fits(value: dict) -> bool:
            """同时校验 Token 与字符预算。"""
            raw = json.dumps(value, ensure_ascii=False, default=str)
            return len(raw) <= char_limit and self.counter(raw) <= budget
        if not fits(result):
            raise ValueError("必要任务约束超过上下文预算")
        # 优先最新结果，旧结果完整保存在 Artifact，失败时仍能按引用恢复。
        result.update(history=[], evidence=[], memory={"summary": data["summary"], "facts": []})
        if not fits(result):
            result["memory"]["summary"] = "摘要已持久化；当前预算不足以装入。"
        for field, values in (("history", histories), ("evidence", evidence)):
            for item in reversed(values):
                candidate = [item, *result[field]]
                result[field] = candidate
                if not fits(result):
                    result[field].pop(0)
                    key = self.artifacts.save(item)
                    reference = {"artifact_id": key, "summary": "旧消息或工具观察，可按引用读取"}
                    result[field].insert(0, reference)
                    if not fits(result):
                        result[field].pop(0)
                    break
        for fact in reversed(data["facts"]):
            result["memory"]["facts"].insert(0, fact)
            if not fits(result):
                result["memory"]["facts"].pop(0)
                break
        self.save(data)
        if not fits(result):
            raise ValueError("无法在保留必要约束的情况下装配上下文")
        self.last_usage = {"role": role, "input_tokens": self.counter(json.dumps(result, ensure_ascii=False)),
                           "budget": budget, "counter": "configured", "compressed": data["compressed"]}
        return result
