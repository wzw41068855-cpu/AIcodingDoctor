"""分域解析凭据；仅通过子进程环境传递，禁止写入任务配置。"""

import os


def credential(domain: str) -> str:
    """按服务专用、页面、兼容凭据的顺序解析。

    Args:
        domain: LLM、EMBEDDING 或 RERANK。
    Returns:
        凭据字符串；未配置时为空。
    """
    return (os.getenv(f"AICD_{domain}_API_KEY") or os.getenv("AICD_WEB_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY", ""))
