"""构建产物内容校验，不导入插件或读取运行配置。"""
import argparse
import ast
import hashlib
import tarfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="校验 wheel 与源码包的独立性和内容一致性")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wheel = arguments.dist / "nonebot_plugin_group_guard-1.0.1-py3-none-any.whl"
    source = arguments.dist / "nonebot_plugin_group_guard-1.0.1.tar.gz"
    forbidden = ["little" + "bot", "Hamu" + "Chan"]
    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert len(names) == len(set(names))
        for name in names:
            if name.endswith("/"):
                continue
            parts = Path(name).parts
            assert "data" not in parts and "__pycache__" not in parts
            assert not any(part.startswith(".env") for part in parts)
            if name.endswith((".py", ".md", "METADATA")):
                text = archive.read(name).decode("utf-8")
                assert not any(word.casefold() in text.casefold() for word in forbidden), name
            if name.startswith(("nonebot_plugin_group_guard/", "nonebot_group_guard_migrate/")):
                assert archive.read(name) == (root / "src" / name).read_bytes(), name
                if name.endswith(".py"):
                    tree = ast.parse(archive.read(name))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom):
                            assert not (node.module or "").startswith(("src.core", "src.plugins")), name
        assert any(name.endswith("licenses/astrbot_plugin_joinmanager.txt") for name in names)
        assert any(name.endswith("licenses/astrbot_plugin_group_combined_captcha_verify.txt") for name in names)
        print("WHEEL_CONTENT_OK", len(names))
    with tarfile.open(source) as archive:
        names = archive.getnames()
        for name in names:
            if name.endswith("/"):
                continue
            parts = Path(name).parts
            assert not any(part in {"data", "output", ".venv", "__pycache__"} for part in parts)
            assert not any(part.startswith(".env") for part in parts)
        assert any(name.endswith("tests/test_group_guard_service.py") for name in names)
        assert any(name.endswith("docs/approval-rules.md") for name in names)
        print("SDIST_CONTENT_OK", len(names))
    for path in (wheel, source):
        print(path.name, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
