"""记录任务失败阶段与安全栈位置，避免将 SDK 异常中的凭据写入日志。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import traceback


def record_failure(directory: Path, exc: Exception, stage: str) -> dict:
    """追加任务诊断并生成可供网页显示的安全错误信息。

    Args:
        directory: 当前任务的独立产物目录。
        exc: 捕获的异常；仅白名单业务错误可以展示原始说明。
        stage: 运行时提供的固定阶段名，不接受模型或用户文本。
    Returns:
        error、error_type、error_stage、diagnostics 和 diagnostic_path 字段。
        日志写入失败时返回 diagnostic_error，不覆盖原始失败信息。
    Notes:
        不保存异常原文、源码行或局部变量。未知异常只记录类型和栈位置，
        防止模型 SDK 错误携带 API Key、请求正文或用户代码。
    """
    reason = ""
    if type(exc) is ValueError and str(exc) == "源仓库必须干净；不会忽略未提交或未跟踪文件":
        reason = "源仓库存在未提交修改或未跟踪文件。请先检查 git status，将无须版本管理的文件加入 .gitignore，并由你确认、提交需要保留的改动后重新创建任务。"
    details = {"time": datetime.now(timezone.utc).isoformat(), "stage": stage,
               "error_type": type(exc).__name__, "reason": reason,
               "frames": [{"file": Path(frame.f_code.co_filename).name,
                           "line": line, "function": frame.f_code.co_name}
                          for frame, line in traceback.walk_tb(exc.__traceback__)]}
    def children(error, depth=0):
        """展开异常组与因果链，只保存类型及栈位置，限制深度避免循环链。"""
        if depth >= 8:
            return []
        nested = list(error.exceptions) if isinstance(error, BaseExceptionGroup) else []
        cause = error.__cause__ or error.__context__
        if cause is not None and cause not in nested:
            nested.append(cause)
        return [{"error_type": type(item).__name__,
                 "frames": [{"file": Path(frame.f_code.co_filename).name, "line": line,
                             "function": frame.f_code.co_name}
                            for frame, line in traceback.walk_tb(item.__traceback__)],
                 "children": children(item, depth + 1)} for item in nested[:16]]
    details["children"] = children(exc)
    path = directory / "diagnostics.jsonl"
    result = {"error": f"任务退出：{type(exc).__name__}；阶段：{stage}" + ("；" + reason if reason else "；请查看任务诊断中的栈位置"),
              "error_type": type(exc).__name__, "error_stage": stage,
              "diagnostics": details, "diagnostic_path": str(path)}
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(details, ensure_ascii=False) + "\n")
    except OSError:
        result["diagnostic_error"] = "诊断日志写入失败，诊断信息仍保存在任务记录中"
    return result
