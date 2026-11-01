"""为本地网页提供文件夹选择和只读候选项，不加载仓库源码或凭据。"""

import json
import os
from pathlib import Path
import subprocess
import sys

from workspace.manager import git, safe_relative


def pick_repository():
    """在独立进程打开系统目录对话框，避免 Tk 跨 Web 线程复用。

    Returns:
        用户选择的绝对路径；取消时返回 None。
    Raises:
        RuntimeError: 图形环境不可用、对话框超时或子进程失败。
    Notes:
        不上传文件，不修改仓库；对话框最多等待五分钟。
        子进程仅接收运行 Tk 所需环境，不继承模型凭据。
    """
    names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
             "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "TCL_LIBRARY", "TK_LIBRARY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run([sys.executable, "-m", "web_runtime.options"],
            env=env, capture_output=True, timeout=300, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise RuntimeError("无法打开本机文件夹选择器")
        return json.loads(result.stdout.decode("utf-8")) or None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError("文件夹选择失败或超时，请重试") from exc


def repository_options(repository):
    """枚举已跟踪的业务 Python 文件与测试候选项，仅返回安全相对路径。

    Args:
        repository: 本机对话框选中的 Git 仓库根目录。
    Returns:
        repository、allowed_files 和 tests；未提交文件不列为修复候选。
    Raises:
        ValueError: 目录不是 Git 根、包含链接或候选清单过大。
        RuntimeError: Git 查询失败。
    """
    supplied = Path(repository).absolute()
    if any(p.is_symlink() or p.is_junction() for p in [supplied, *supplied.parents]):
        raise ValueError("不支持链接形式的仓库路径")
    root = supplied.resolve(strict=True)
    if Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise ValueError("请选择 Git 仓库根目录")
    files = git(root, "ls-files", "-z").split("\0")
    if len(files) > 10000:
        raise ValueError("仓库清单超过 10000 项，请缩小仓库范围")
    source, tests = [], set()
    excluded = {"conftest.py", "setup.py", "config.py", "settings.py", "key.py", "keys.py"}
    ignored = {"venv", "__pycache__", "node_modules", "site-packages", "build", "dist"}
    for relative in files:
        try:
            parts = safe_relative(relative)
        except ValueError:
            continue
        target = root.joinpath(*parts)
        if (target.suffix != ".py" or not target.is_file()
                or any(p.lower() in ignored for p in parts)
                or any(p.is_symlink() or p.is_junction() for p in [target, *target.parents])):
            continue
        is_test = target.name.startswith("test_") or target.name.endswith("_test.py")
        in_tests = any(p.lower() in {"test", "tests"} for p in parts[:-1])
        if is_test and target.name.startswith("test_"):
            tests.add(relative)
            for index, part in enumerate(parts[:-1]):
                if part.lower() in {"test", "tests"}:
                    tests.add("/".join(parts[:index + 1]))
        if not is_test and not in_tests and target.name not in excluded and target.stat().st_nlink == 1:
            source.append(relative)
    return {"repository": str(root), "allowed_files": sorted(source), "tests": sorted(tests)}


def local_images():
    """查询本地 Docker 镜像标签和 ID；不拉取镜像，不启动容器。

    Returns:
        images 列表及可供页面展示的 error；Docker 不可用时列表为空。
    """
    try:
        from sandbox.docker_runner import capture
        code, output, truncated = capture(["docker", "image", "ls", "--format",
            "{{.Repository}}:{{.Tag}}\t{{.ID}}"], 10, limit=65536)
        if code or truncated:
            return {"images": [], "error": "无法获取完整本地镜像列表，请检查 Docker"}
        images = set()
        for line in output.decode("utf-8", "replace").splitlines():
            tag, image_id = line.split("\t", 1)
            images.add(image_id if "<none>" in tag else tag)
        return {"images": sorted(images), "error": None}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"images": [], "error": "Docker 不可用，请启动 Docker 后刷新镜像"}


if __name__ == "__main__":
    import tkinter as tk
    from tkinter import filedialog
    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        print(json.dumps(filedialog.askdirectory(parent=window, title="选择本地 Python Git 仓库", mustexist=True)))
    finally:
        window.destroy()
