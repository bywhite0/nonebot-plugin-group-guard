"""群管 JSON 状态与原子写入，不依赖 NoneBot。"""
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

NOTICE_LIMIT = 2000
_STATE_TABLES = ('records', 'sessions', 'bypass', 'requests', 'notices')
T = TypeVar('T')


def _atomic_write_json(path: Path, data: Any, *, exclusive: bool = False) -> None:
    """同目录写临时文件，刷盘成功后替换；迁移可要求目标此前不存在。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    reserved: tuple[int, int] | None = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n', dir=path.parent,
            prefix=f'.{path.name}.', suffix='.tmp', delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            # 独占创建占位文件，不能把迁移期间新出现的业务文件覆盖掉。
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                stat = os.fstat(descriptor)
                reserved = (stat.st_dev, stat.st_ino)
            finally:
                os.close(descriptor)
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != reserved:
                raise FileExistsError('迁移目标已被其他写入者替换')
        os.replace(temporary, path)
        replaced = True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if reserved is not None and not replaced:
            try:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) == reserved:
                    path.unlink()
            except FileNotFoundError:
                pass


def _normalize_state(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError('群管状态必须是 JSON 对象')
    result = deepcopy(data)
    for name in _STATE_TABLES:
        table = result.setdefault(name, {})
        if not isinstance(table, dict):
            raise ValueError(f'群管状态表 {name} 必须是 JSON 对象')
    notices = result['notices']
    if len(notices) > NOTICE_LIMIT:
        result['notices'] = dict(list(notices.items())[-NOTICE_LIMIT:])
    return result


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = _normalize_state({})
        self._lock = RLock()

    def load(self) -> dict:
        """损坏文件抛错，不触碰原文件与已加载的状态。"""
        with self._lock:
            try:
                with self.path.open(encoding='utf-8-sig') as handle:
                    raw = json.load(handle)
            except FileNotFoundError:
                candidate = _normalize_state({})
            else:
                candidate = _normalize_state(raw)
            self.data = candidate
            return self.data

    def save(self) -> None:
        with self._lock:
            candidate = _normalize_state(self.data)
            _atomic_write_json(self.path, candidate)
            self.data = candidate

    def transaction(self, mutator: Callable[[dict], T]) -> T:
        """修改隔离副本，写盘失败或回调异常都不发布到内存。"""
        with self._lock:
            candidate = deepcopy(self.data)
            result = mutator(candidate)
            candidate = _normalize_state(candidate)
            _atomic_write_json(self.path, candidate)
            self.data = candidate
            return result
