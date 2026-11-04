"""离线迁移 CLI，不初始化 NoneBot 或注册插件。"""
import importlib.util
from pathlib import Path


def main():
    package = importlib.util.find_spec("nonebot_plugin_group_guard")
    if package is None or not package.submodule_search_locations:
        raise RuntimeError("未安装群管插件")
    path = Path(next(iter(package.submodule_search_locations))) / "migration.py"
    spec = importlib.util.spec_from_file_location("_group_guard_migration_entry", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载群管迁移入口")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
