"""可信宿主绑定的角色权限，模型只能选择白名单内动作。"""

READ = {"list_files", "read_file", "find_symbol", "search_code", "search_context", "find_references", "read_artifact", "git_status", "git_log"}
ROLE_TOOLS = {
    "Supervisor": set(),
    "Explorer": READ,
    "Coder": READ | {"snapshot", "preview_patch", "apply_patch", "sync_changed_files", "current_changes", "git_diff"},
    "Tester": {"list_files", "read_file", "read_artifact", "run_pytest", "add_regression_test"},
    "Reviewer": {"read_file", "read_artifact", "git_diff", "current_changes"},
    "runtime": READ | {"git_diff", "current_changes"},
}


def authorize(role: str, tool: str) -> None:
    """在调用前拒绝不属于角色的工具。

    Args:
        role: 宿主阶段绑定的角色。
        tool: 已注册的工具名。
    Raises:
        ValueError: 操作不被允许。
    """
    if tool not in ROLE_TOOLS.get(role, set()):
        raise ValueError("角色无权执行该工具")
