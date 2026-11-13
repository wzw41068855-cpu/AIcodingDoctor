"""只接受已读取且版本仍一致的源码引用。"""

import re
from pathlib import Path

from services.code_search import CodeSearchService


def validate_citations(answer: str, facts: list[dict], root: Path) -> list[dict]:
    """检查回答中 path.py:line 引用的来源和当前版本。

    Args:
        answer: 最终答案。
        facts: 工具产生的结构化事实。
        root: 授权仓库。
    Returns:
        已核验的引用。
    Raises:
        ValueError: 引用不存在、过期、越界或未曾读取。
    """
    matches = re.findall(r"([\w./-]+\.(?:py|md)):(\d+)(?:[-–](\d+))?", answer)
    if not matches:
        raise ValueError("代码回答缺少可核验的文件:行号引用")
    service = CodeSearchService(root)
    citations = []
    for path, first, last in matches:
        start, end = int(first), int(last or first)
        current = service.get_code_context(path)
        service.get_code_context(path, start, end)
        if not any(f.get("relative_path") == path and f.get("start_line", 0) <= start
                   and f.get("end_line", 0) >= end and f.get("source_version") == current.source_version for f in facts):
            raise ValueError("引用未读取或版本已变化：" + path)
        citations.append({"path": path, "start_line": start, "end_line": end, "source_version": current.source_version})
    return citations
