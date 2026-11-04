"""群管配置、群级继承与安全的本机持久化。"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictStr, field_validator

from .store import _atomic_write_json


class _PreservingModel(BaseModel):
    # 旧插件会附带模板元数据与群级扩展项，不能在迁移或保存时静默丢弃。
    model_config = ConfigDict(extra='allow', hide_input_in_errors=True)


def _identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('账号或群号必须是正整数或其字符串')
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError('账号或群号必须是正整数或其字符串')
    return text


class Config(BaseModel):
    group_guard_config_file: Path = Path('data/group_guard/config.json')
    group_guard_state_file: Path = Path('data/group_guard/state.json')


class ApprovalRule(_PreservingModel):
    model_config = ConfigDict(revalidate_instances='always')

    name: StrictStr = Field(min_length=1)
    match: Literal['exact', 'contains'] = 'exact'
    values: list[StrictStr] = Field(min_length=1, strict=True)
    case_sensitive: bool = Field(default=False, strict=True)
    source: StrictStr | None = None

    @field_validator('name')
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('规则名称不能为空白')
        return value

    @field_validator('values')
    @classmethod
    def _validate_values(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError('规则答案不能为空白')
        return value


class _ApprovalFields(_PreservingModel):
    # 群级只保留实际配置的字段，复用同一份校验而不写入全局缺省值。
    approval_mode: Literal['legacy', 'rules', 'manual'] = 'legacy'
    accept_rules: list[ApprovalRule] = Field(default_factory=list, strict=True)
    source_statistics: bool | None = Field(default=None, strict=True)
    accept_categories: list[str] = Field(default_factory=list)
    reject_key: list[str] = Field(default_factory=list)

    @field_validator('accept_rules')
    @classmethod
    def _validate_rule_names(cls, value: list[ApprovalRule]) -> list[ApprovalRule]:
        names = [rule.name for rule in value]
        if len(names) != len(set(names)):
            raise ValueError('允许规则名称不能重复')
        return value


class ApprovalConfig(_ApprovalFields):
    group_rules: dict[str, dict] = Field(default_factory=dict)
    divide_group: dict[str, Any] = Field(default_factory=lambda: {
        'block_method': 'whitelist', 'control_list': [], 'disabled_statistics': [],
    })
    notice: dict[str, list[str]] = Field(default_factory=lambda: {
        'accept_notice': [], 'reject_notice': [], 'increase_notice': [], 'decrease_notice': [],
    })
    msg: dict[str, list[str]] = Field(default_factory=lambda: {
        'welcome_msg': ['default:欢迎新成员！通过自动审核'],
        'reject_reason': ['default:检测到关键词%key%，拒绝申请'],
        'decrease_msg': ['default:%user_name%(%user_id%)退出了群聊'],
        'increase_msg': ['default:恭喜你通过人工审核，欢迎入群~'],
    })
    delay: float = Field(default=0.5, ge=0)
    font: str = 'cute_font.ttf'
    bg_img: str = 'bg.jpg'
    assets_dir: str = ''

    @field_validator('group_rules')
    @classmethod
    def _validate_group_rules(cls, value: dict[str, dict]) -> dict[str, dict]:
        result = {}
        for gid, rule in value.items():
            _identifier(gid)
            for key in ('accept_categories', 'reject_key'):
                if key not in rule:
                    continue
                entries = rule[key]
                if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
                    raise ValueError(f'群级 {key} 必须是字符串列表')
            validated = _ApprovalFields.model_validate(rule)
            overrides = validated.model_dump(exclude_unset=True)
            if 'accept_rules' in rule:
                overrides['accept_rules'] = [item.model_dump() for item in validated.accept_rules]
            result[gid] = overrides
        return result

    @field_validator('divide_group')
    @classmethod
    def _validate_scope(cls, value: dict) -> dict:
        method = value.get('block_method', 'whitelist')
        if method not in {'whitelist', 'blacklist'}:
            raise ValueError('审批范围只支持 whitelist 或 blacklist')
        for name in ('control_list', 'disabled_statistics'):
            entries = value.get(name, [])
            if not isinstance(entries, list):
                raise ValueError(f'{name} 必须是列表')
            for entry in entries:
                _identifier(entry)
        return value

    def allows(self, gid: int | str) -> bool:
        scope = self.divide_group
        in_list = str(gid) in {str(item) for item in scope.get('control_list', [])}
        if scope.get('block_method', 'whitelist') == 'blacklist':
            return not in_list
        return in_list

    def for_group(self, gid: int | str) -> dict:
        result = self.model_dump(exclude={'group_rules'})
        result.update(self.group_rules.get(str(gid), {}))
        return deepcopy(result)

    def source_enabled(self, gid: int | str) -> bool:
        disabled = {str(item) for item in self.divide_group.get('disabled_statistics', [])}
        if not self.allows(gid) or str(gid) in disabled:
            return False
        config = self.for_group(gid)
        enabled = config['source_statistics']
        return config['approval_mode'] == 'legacy' if enabled is None else enabled

    def chart_enabled(self, gid: int | str) -> bool:
        return self.source_enabled(gid)


class GroupVerificationConfig(_PreservingModel):
    group_id: str
    enabled: bool = False
    verification_timeout: int | None = Field(default=None, gt=0)
    max_wrong_answers: int | None = Field(default=None, gt=0)
    kick_delay: int | None = Field(default=None, ge=0)
    enable_combined_verify: bool | None = None
    enable_level_verify: bool | None = None
    min_qq_level: int | None = Field(default=None, ge=0)
    verify_delay: int | None = Field(default=None, ge=0)
    recall_unverified_messages: bool | None = None
    warning_time: int | None = Field(default=None, ge=0)
    new_member_prompt: str | None = None
    welcome_message: str | None = None
    wrong_answer_prompt: str | None = None
    error_verification: str | None = None
    failure_message: str | None = None
    kick_message: str | None = None
    too_many_wrong_message: str | None = None
    countdown_warning_prompt: str | None = None
    web_member_prompt: str | None = None
    web_wrong_answer_prompt: str | None = None

    @field_validator('group_id', mode='before')
    @classmethod
    def _validate_group_id(cls, value: Any) -> str:
        return _identifier(value)


class VerificationConfig(_PreservingModel):
    verification_timeout: int = Field(default=300, gt=0)
    kick_delay: int = Field(default=5, ge=0)
    max_wrong_answers: int = Field(default=5, gt=0)
    api_base_url: str = ''
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(''))
    enable_combined_verify: bool = False
    enable_level_verify: bool = False
    min_qq_level: int = Field(default=20, ge=0)
    verify_delay: int = Field(default=3, ge=0)
    warning_time: int = Field(default=60, ge=0)
    recall_unverified_messages: bool = False
    new_member_prompt: str = (
        '{at_user} 欢迎加入本群！请在 {timeout} 分钟内回答下面的问题以完成验证：\n{question}'
    )
    welcome_message: str = '{at_user} 验证成功，欢迎你的加入！'
    wrong_answer_prompt: str = '{at_user} 答案错误，请重新回答验证。这是你的新问题：\n{question}'
    error_verification: str = '{at_user} 你还未完成验证。请在 {timeout} 分钟内输入验证码以完成验证'
    failure_message: str = '{at_user} 很抱歉，您对验证的响应超时，您将在 {countdown} 秒后被请出本群。'
    kick_message: str = '{at_user} 很抱歉，您因未在规定时间内完成验证，已被请出本群。'
    too_many_wrong_message: str = '{at_user} 很抱歉，你已连续回答错误 {count} 次，将被请出本群。'
    countdown_warning_prompt: str = '{at_user} 验证即将超时，请尽快完成验证。'
    web_member_prompt: str = ('{at_user} 请在 {timeout_seconds} 秒内打开以下链接完成人机验证，再在群内发送六位验证码：\n{url}')
    web_wrong_answer_prompt: str = ('{at_user} 验证码错误，请使用新链接重试，剩余 {remaining} 次：\n{url}')
    group_configs: list[GroupVerificationConfig] = Field(default_factory=list)

    def for_group(self, gid: int | str) -> dict:
        result = self.model_dump(exclude={'api_key', 'api_base_url', 'group_configs'})
        result['enabled'] = False
        for group in self.group_configs:
            if group.group_id != str(gid):
                continue
            overrides = group.model_dump(exclude_none=True)
            # 群级旧扩展仍保留，但后端凭据不进入提供给业务处理器的字典。
            for key in ('api_key', 'api_base_url', 'group_configs'):
                overrides.pop(key, None)
            result.update(overrides)
            break
        return deepcopy(result)


class GuardSettings(_PreservingModel):
    enabled: bool = False
    self_ids: list[str] = Field(default_factory=list)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)

    @field_validator('self_ids', mode='before')
    @classmethod
    def _validate_self_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError('self_ids 必须是列表')
        return [_identifier(item) for item in value]


def _local_payload(value: Any) -> Any:
    """只在本机 JSON 落盘时展开 SecretStr，普通 repr 与 model_dump 不泄密。"""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, BaseModel):
        return _local_payload(value.model_dump(mode='python'))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _local_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_local_payload(item) for item in value]
    return value


class ConfigRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.settings = GuardSettings()
        self._lock = RLock()

    def load(self) -> GuardSettings:
        with self._lock:
            try:
                with self.path.open(encoding='utf-8-sig') as handle:
                    raw = json.load(handle)
            except FileNotFoundError:
                candidate = GuardSettings()
            else:
                candidate = GuardSettings.model_validate(raw)
            self.settings = candidate
            return self.settings

    def save(self, settings: GuardSettings) -> None:
        with self._lock:
            # 再校验并断开调用方引用，避免保存后被原对象直接修改。
            candidate = GuardSettings.model_validate(deepcopy(settings.model_dump(mode='python')))
            _atomic_write_json(self.path, _local_payload(candidate))
            self.settings = candidate

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            candidate = self.settings.model_dump(mode='python')
            candidate['enabled'] = enabled
            self.save(GuardSettings.model_validate(candidate))

    def update_group(self, gid: int | str, **changes: Any) -> None:
        identifier = _identifier(gid)
        if 'group_id' in changes:
            raise ValueError('不能通过群配置更新更换 group_id')
        with self._lock:
            candidate = deepcopy(self.settings.model_dump(mode='python'))
            groups = candidate['verification']['group_configs']
            for group in groups:
                if group['group_id'] == identifier:
                    group.update(changes)
                    break
            else:
                groups.append({'group_id': identifier, **changes})
            self.save(GuardSettings.model_validate(candidate))

    def update_approval_group(self, gid: int | str, **changes: Any) -> None:
        identifier = _identifier(gid)
        with self._lock:
            candidate = deepcopy(self.settings.model_dump(mode='python'))
            rules = candidate['approval']['group_rules']
            rules.setdefault(identifier, {}).update(changes)
            self.save(GuardSettings.model_validate(candidate))
