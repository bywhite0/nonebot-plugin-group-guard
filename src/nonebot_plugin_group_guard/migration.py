"""将指定 AstrBot 两插件迁入群管配置；不启动宿主、不接触 QQ。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# 直接按文件执行 CLI 时建立仅用于相对导入的包，不执行插件入口。
if not __package__:
    import types

    _package_name = '_group_guard_migration_cli'
    _package = types.ModuleType(_package_name)
    _package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[_package_name] = _package
    __package__ = _package_name

from .config import ApprovalConfig, GuardSettings, VerificationConfig, _local_payload
from .store import _atomic_write_json, _normalize_state

JOIN_PLUGIN = 'astrbot_plugin_joinmanager'
VERIFY_PLUGIN = 'astrbot_plugin_group_combined_captcha_verify'
MAX_BACKGROUND_BYTES = 256 * 1024 * 1024
MAX_BACKGROUND_FILES = 1000


def _read_object(path: Path) -> dict:
    with path.open(encoding='utf-8-sig') as handle:
        result = json.load(handle)
    if not isinstance(result, dict):
        raise ValueError('旧插件配置或记录必须是 JSON 对象')
    return result


def _read_records(path: Path) -> dict:
    if not path.exists():
        return {}
    records = _read_object(path)
    for group in records.values():
        if not isinstance(group, dict):
            raise ValueError('旧来源记录的群数据必须是 JSON 对象')
        if any(not isinstance(details, dict) for details in group.values()):
            raise ValueError('旧来源记录的成员详情必须是 JSON 对象')
    return records


def _source_asset(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('素材路径不能越出旧插件 assets 目录')
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError('素材路径不能越出旧插件 assets 目录')
    return candidate


def _asset_plan(root: Path, approval: ApprovalConfig) -> tuple[list[Path], int, int, int]:
    """先计量背景，再决定是否复制；只纳入字体、选定背景及背景目录。"""
    root = root.resolve()
    # 原实例使用背景目录软链接；只引用现有目标，不复制跨目录素材。
    background = root / 'background'
    backgrounds: list[Path] = []
    if background.is_dir():
        for path in background.rglob('*'):
            if path.is_symlink() and not background.is_symlink() and not path.resolve().is_relative_to(root):
                raise ValueError('背景目录不能引用 assets 之外的素材')
            if path.is_file():
                backgrounds.append(path)
    background_bytes = sum(path.stat().st_size for path in backgrounds)
    selected: list[Path] = []
    missing = 0
    for value in (approval.font, approval.bg_img):
        if not value:
            continue
        path = _source_asset(root, value)
        if path.is_file():
            selected.append(path)
        elif path.exists():
            raise ValueError('选定字体与背景必须是文件')
        else:
            missing += 1
    files = list(dict.fromkeys(selected + backgrounds))
    return files, len(backgrounds), background_bytes, missing


def migrate(astrbot_root: Path, output_dir: Path, *, copy_assets: bool = True) -> dict:
    """校验与计量全部成功后迁移；已有目标一律拒绝，不读取独立数学 PRO。"""
    root = Path(astrbot_root).resolve()
    output = Path(output_dir).resolve()
    if output.is_relative_to(root):
        raise ValueError('迁移输出不能位于只读的 AstrBot 原目录内')
    config_target = output / 'config.json'
    state_target = output / 'state.json'
    for target in (config_target, state_target):
        if target.exists() or target.is_symlink():
            raise FileExistsError('迁移目标已有配置或状态，拒绝覆盖')
    approval = ApprovalConfig.model_validate(_read_object(root / 'data/config' / f'{JOIN_PLUGIN}_config.json'))
    verification = VerificationConfig.model_validate(
        _read_object(root / 'data/config' / f'{VERIFY_PLUGIN}_config.json')
    )
    records = _read_records(root / 'data/plugin_data' / JOIN_PLUGIN / 'join_records.json')
    source_assets = root / 'data/plugins' / JOIN_PLUGIN / 'assets'
    files, background_files, background_bytes, missing_assets = _asset_plan(source_assets, approval)
    should_copy = (copy_assets and not (source_assets / 'background').is_symlink()
                   and background_files <= MAX_BACKGROUND_FILES and background_bytes <= MAX_BACKGROUND_BYTES)
    assets_target = output / 'assets'
    if should_copy and (assets_target.exists() or assets_target.is_symlink()):
        raise FileExistsError('迁移目标已有素材，拒绝覆盖')
    approval.assets_dir = str(assets_target.resolve() if should_copy else source_assets.resolve())
    settings = GuardSettings(enabled=False, self_ids=[], approval=approval, verification=verification)
    state = _normalize_state({'records': records})
    summary: dict[str, Any] = {
        'accept_categories': len(approval.accept_categories),
        'reject_keywords': len(approval.reject_key),
        'approval_groups': len(approval.divide_group.get('control_list', [])),
        'verification_groups': len(verification.group_configs),
        'record_groups': len(records),
        'records': sum(len(group) for group in records.values()),
        'assets_mode': 'copied' if should_copy else 'referenced',
        'background_files': background_files,
        'background_bytes': background_bytes,
        'assets_copied': len(files) if should_copy else 0,
        'assets_missing': missing_assets,
    }
    # 新目录内暂存素材；只有本轮成功发布的目标才会在异常时回滚。
    output_existed = output.exists()
    output.mkdir(parents=True, exist_ok=True)
    committed: list[tuple[Path, tuple[int, int]]] = []
    created_assets = False
    try:
        with tempfile.TemporaryDirectory(prefix='.group-guard-migration-', dir=output) as directory:
            stage = Path(directory)
            if should_copy:
                staged_assets = stage / 'assets'
                staged_assets.mkdir()
                for source in files:
                    relative = source.relative_to(source_assets.resolve())
                    destination = staged_assets / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                # exist_ok=False 确保后出现的素材目录也不会被覆盖。
                assets_target.mkdir(exist_ok=False)
                created_assets = True
                for source in staged_assets.iterdir():
                    shutil.move(str(source), str(assets_target / source.name))
            for target, payload in ((config_target, _local_payload(settings)), (state_target, state)):
                _atomic_write_json(target, payload, exclusive=True)
                stat = target.stat()
                committed.append((target, (stat.st_dev, stat.st_ino)))
    except BaseException:
        for path, identity in reversed(committed):
            try:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        if created_assets:
            shutil.rmtree(assets_target)
        if not output_existed:
            try:
                output.rmdir()
            except OSError:
                pass
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='迁移加群请求管理器与合并极验配置；目标必须尚未存在。')
    parser.add_argument('--astrbot-root', type=Path, required=True, help='AstrBot 根目录')
    parser.add_argument('--output-dir', type=Path, required=True, help='群管迁移输出目录')
    parser.add_argument('--no-copy-assets', action='store_true', help='只引用原素材目录，不复制')
    arguments = parser.parse_args(argv)
    try:
        summary = migrate(arguments.astrbot_root, arguments.output_dir,
                          copy_assets=not arguments.no_copy_assets)
    except FileExistsError:
        parser.exit(1, '迁移失败：目标已存在，拒绝覆盖。\n')
    except FileNotFoundError:
        parser.exit(1, '迁移失败：缺少指定插件的配置或素材。\n')
    except ValueError:
        parser.exit(1, '迁移失败：配置、记录或素材路径无效；原文件未改动。\n')
    except OSError:
        parser.exit(1, '迁移失败：磁盘读写异常；请检查目录权限与可用空间。\n')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
