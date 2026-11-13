"""Path structure checks, without file-type or operation permissions.

These checks do not provide atomic access or eliminate TOCTOU races.
"""

from pathlib import Path, PureWindowsPath


def safe_relative(value: str) -> tuple[str, ...]:
    """Return normalized components, rejecting traversal and Windows drives."""
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (not value or "\x00" in value or normalized.startswith("/")
            or PureWindowsPath(value).drive or ":" in value
            or any(p in {"", ".", ".."} for p in parts)):
        raise ValueError("不安全的相对路径")
    return tuple(parts)


def reject_links(path: Path) -> None:
    """Reject symlinks and junctions in the supplied path and its ancestors."""
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("拒绝符号链接或目录联接")


def resolve_within(root: Path, relative: str, *, strict: bool = True,
                   allow_links: bool = False) -> Path:
    """Resolve beneath root; read-only callers may explicitly allow links.

    Callers allowing links must also authorize the resolved target's name/type.
    strict=False preserves read-only callers' handling of missing files.
    """
    parts = safe_relative(relative)
    if not allow_links:
        reject_links(root)
    root = root.resolve(strict=strict)
    candidate = root.joinpath(*parts)
    if not allow_links:
        reject_links(candidate)
    resolved = candidate.resolve(strict=strict)
    if not resolved.is_relative_to(root):
        raise ValueError("路径越出允许目录")
    return resolved


def ensure_separate(first: Path, second: Path) -> None:
    """Reject equal or nested directories, including unresolved aliases."""
    first, second = first.resolve(), second.resolve()
    if first.is_relative_to(second) or second.is_relative_to(first):
        raise ValueError("目录必须相互隔离")
