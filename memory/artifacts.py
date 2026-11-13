"""大型工具结果按内容寻址保存，引用不能指定任意文件路径。"""

import json
from pathlib import Path
import re
from typing import Any
import config

from graph.operations import digest
from graph.test_runner import redact_data


class Artifacts:
    """任务独立的 JSON 工具产物。"""

    def __init__(self, directory: Path) -> None:
        """绑定任务目录。

        Args:
            directory: 可信宿主指定的产物目录。
        """
        self.root = directory / "tool_artifacts"
        self.root.mkdir(exist_ok=True)

    def save(self, value: Any) -> str:
        """脱敏后保存完整结果。

        Args:
            value: JSON 数据。
        Returns:
            内容哈希引用。
        """
        value = redact_data(value)
        key = digest(value)
        (self.root / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return key

    def read(self, key: str, offset: int = 0, max_chars: int = config.ARTIFACT_READ_CHARS) -> dict:
        """按有界窗口读取，不接受用户路径。

        Args:
            key: 内容哈希。
            offset: 字符偏移。
            max_chars: 读取窗口，最多 6000 字符。
        Returns:
            原始 JSON 文本片段及后续偏移。
        """
        if not re.fullmatch(r"[a-f0-9]{64}", key) or offset < 0 or not 1 <= max_chars <= config.ARTIFACT_MAX_READ_CHARS:
            raise ValueError("产物引用或范围无效")
        path = self.root / f"{key}.json"
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError("产物路径被替换")
        raw = path.read_text(encoding="utf-8")
        if digest(json.loads(raw)) != key:
            raise ValueError("产物内容已变化")
        return {"artifact_id": key, "content": raw[offset:offset + max_chars],
                "next_offset": min(len(raw), offset + max_chars), "truncated": offset + max_chars < len(raw)}
