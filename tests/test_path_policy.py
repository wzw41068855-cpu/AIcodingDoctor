"""Shared primitives and preserved caller-specific security boundaries."""

from pathlib import Path

import pytest

from security.path_policy import ensure_separate, reject_links, resolve_within, safe_relative
from workspace.manager import safe_relative as task_relative


@pytest.mark.parametrize("value", ["", "../a.py", "a/../b.py", "/a.py", "C:a.py",
                                  "C:/a.py", "\\\\server\\share\\a.py", "a//b.py",
                                  "./a.py", "a/", "a\x00.py", "a.py:stream"])
def test_invalid_relative(value):
    with pytest.raises(ValueError):
        safe_relative(value)


def test_normal_paths_and_missing(tmp_path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "a.py"
    target.write_text("x = 1\n")
    assert safe_relative("src\\a.py") == ("src", "a.py")
    assert resolve_within(tmp_path, "src/a.py") == target.resolve()
    with pytest.raises(FileNotFoundError):
        resolve_within(tmp_path, "missing.py")
    assert resolve_within(tmp_path, "missing.py", strict=False).name == "missing.py"


@pytest.mark.parametrize("value", [".hidden/a.py", "secrets/a.py", "keys.py", "a.pem", "a\\b.py"])
def test_task_rules_remain_stricter(value):
    safe_relative(value)
    with pytest.raises(ValueError):
        task_relative(value)


@pytest.mark.parametrize("nested", ["same", "child", "parent"])
def test_overlapping_directories(tmp_path, nested):
    child = tmp_path / "child"
    first, second = {"same": (tmp_path, tmp_path), "child": (tmp_path, child),
                     "parent": (child, tmp_path)}[nested]
    with pytest.raises(ValueError):
        ensure_separate(first, second)
    ensure_separate(tmp_path / "a", tmp_path / "ab")


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_link_detection_includes_parents(tmp_path, monkeypatch, kind, ancestor):
    link = tmp_path / "link"
    candidate = link / "a.py" if ancestor else link
    monkeypatch.setattr(Path, kind, lambda self: self == link)
    with pytest.raises(ValueError):
        reject_links(candidate)


def test_real_links_and_containment(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.py"
    target.write_text("x = 1\n")
    alias = root / "alias.py"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ValueError):
        resolve_within(root, "alias.py")
    assert resolve_within(root, "alias.py", allow_links=True) == target.resolve()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 2\n")
    (root / "escape.py").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_within(root, "escape.py", allow_links=True)


@pytest.mark.parametrize("relative", [".hidden/a.py", "secrets/a.py", "credentials.md", "a.txt"])
def test_loader_business_rules(tmp_path, relative):
    from rag.loader import Loader
    with pytest.raises(ValueError):
        Loader(str(tmp_path)).resolve_path(relative)


def test_loader_allows_markdown_and_internal_alias(tmp_path):
    from rag.loader import Loader
    target = tmp_path / "guide.md"
    target.write_text("# Guide\n")
    loader = Loader(str(tmp_path))
    assert loader.read_file("guide.md").page_content == "# Guide\n"
    alias = tmp_path / "alias.md"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    assert loader.resolve_path(str(alias)) == target.resolve()
