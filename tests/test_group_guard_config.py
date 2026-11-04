"""群管配置与状态的离线测试，不注册 NoneBot 插件。"""
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pydantic import SecretStr, ValidationError

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'src/nonebot_plugin_group_guard'
PACKAGE = '_group_guard_data_tests'
SCRATCH = Path(tempfile.gettempdir())


def load_guard(name):
    if not (PLUGIN / f'{name}.py').is_file():
        return None
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f'{PACKAGE}.{name}')


C = load_guard('config')
S = load_guard('store')


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(C, '群管配置模块尚未实现')
        self.assertIsNotNone(S, '群管状态模块尚未实现')
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='group-guard-data-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'

    def test_safe_defaults_and_host_paths(self):
        host = C.Config()
        self.assertEqual(host.group_guard_config_file, Path('data/group_guard/config.json'))
        self.assertEqual(host.group_guard_state_file, Path('data/group_guard/state.json'))
        settings = C.GuardSettings()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.self_ids, [])
        self.assertFalse(settings.approval.allows(1001))
        self.assertFalse(settings.approval.chart_enabled(1001))
        self.assertFalse(settings.verification.for_group(1001)['enabled'])
        self.assertEqual(settings.verification.warning_time, 60)
        self.assertFalse(settings.verification.recall_unverified_messages)

    def test_approval_scope_and_disabled_charts(self):
        config = C.ApprovalConfig(divide_group={
            'block_method': 'whitelist', 'control_list': [1001, '1002'],
            'disabled_statistics': ['1002'], 'legacy_scope': '保留',
        })
        self.assertTrue(config.allows('1001'))
        self.assertTrue(config.allows(1002))
        self.assertFalse(config.allows(1003))
        self.assertTrue(config.chart_enabled(1001))
        self.assertFalse(config.chart_enabled(1002))
        self.assertEqual(config.divide_group['legacy_scope'], '保留')
        block = C.ApprovalConfig(divide_group={
            'block_method': 'blacklist', 'control_list': ['1001'],
        })
        self.assertFalse(block.allows(1001))
        self.assertTrue(block.allows(1002))
        with self.assertRaises(ValidationError):
            C.ApprovalConfig(divide_group={'block_method': 'typo'})

    def test_group_rules_are_optional_scoped_and_persisted(self):
        self.assertTrue(hasattr(C.ApprovalConfig(), 'group_rules'), '尚未实现分群关键词配置')
        self.assertEqual(C.ApprovalConfig().group_rules, {})
        repository = C.ConfigRepository(self.path)
        settings = C.GuardSettings(approval={
            'accept_categories': ['全局:关键词'], 'reject_key': ['全局拒绝'],
            'group_rules': {'1001': {'accept_categories': ['群级:独有'],
                                     'legacy_rule': {'preserve': True}},
                            '1002': {'reject_key': []}},
        })
        repository.save(settings)
        approval = C.ConfigRepository(self.path).load().approval
        self.assertEqual(approval.group_rules['1001']['accept_categories'], ['群级:独有'])
        self.assertNotIn('reject_key', approval.group_rules['1001'])
        self.assertNotIn('accept_categories', approval.group_rules['1002'])
        self.assertEqual(approval.group_rules['1002']['reject_key'], [])
        self.assertEqual(approval.group_rules['1001']['legacy_rule'], {'preserve': True})
        self.assertNotIn('1003', approval.group_rules)
        self.assertEqual(approval.accept_categories, ['全局:关键词'])
        self.assertEqual(approval.reject_key, ['全局拒绝'])

    def test_invalid_group_rules_are_rejected(self):
        for invalid in [
            {'1001': {'accept_categories': '不是列表'}},
            {'1001': {'reject_key': [False]}},
            {'1001': {'reject_key': None}},
            {'0': {'accept_categories': []}},
        ]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    C.ApprovalConfig(group_rules=invalid)

    def test_approval_defaults_keep_legacy_behavior(self):
        config = C.ApprovalConfig()
        self.assertEqual(getattr(config, 'approval_mode', None), 'legacy')
        self.assertEqual(config.accept_rules, [])
        self.assertIsNone(config.source_statistics)
        self.assertEqual(config.group_rules, {})

    def test_approval_rule_defaults_and_explicit_source(self):
        self.assertTrue(hasattr(C, 'ApprovalRule'), '尚未实现允许规则模型')
        rule = C.ApprovalRule(name='固定答案', values=['Alpha', 'Beta'])
        self.assertEqual(rule.model_dump(), {
            'name': '固定答案', 'match': 'exact', 'values': ['Alpha', 'Beta'],
            'case_sensitive': False, 'source': None,
        })
        named = C.ApprovalRule(name='渠道', match='contains', values=['github'],
                               case_sensitive=True, source='GitHub')
        self.assertEqual(named.source, 'GitHub')
        self.assertTrue(named.case_sensitive)

    def test_approval_rules_reject_invalid_or_coerced_fields(self):
        self.assertTrue(hasattr(C, 'ApprovalRule'), '尚未实现允许规则模型')
        invalid_fields = [
            {'name': ''}, {'name': ' \t'}, {'name': 3},
            {'match': 'regex'}, {'match': None},
            {'values': []}, {'values': ['']}, {'values': [' \n']},
            {'values': 'Alpha'}, {'values': ('Alpha',)}, {'values': [True]},
            {'values': [42]}, {'case_sensitive': 'false'}, {'case_sensitive': 1},
            {'case_sensitive': None}, {'source': 42},
        ]
        for invalid in invalid_fields:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    C.ApprovalRule.model_validate({'name': '固定答案', 'values': ['Alpha'], **invalid})

    def test_approval_revalidates_mutated_rule_instances(self):
        rule = C.ApprovalRule(name='假规则', values=['Alpha'])
        rule.values.clear()
        for fields in [{'accept_rules': [rule]},
                       {'group_rules': {'1001': {'accept_rules': [rule]}}}]:
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    C.ApprovalConfig(**fields)

    def test_approval_new_fields_are_strict_globally_and_per_group(self):
        self.assertTrue(hasattr(C.ApprovalConfig(), 'approval_mode'), '尚未实现审批模式')
        invalid_fields = [
            {'approval_mode': 'auto'}, {'approval_mode': None}, {'approval_mode': True},
            {'accept_rules': None}, {'accept_rules': {}}, {'accept_rules': ()},
            {'accept_rules': [{'name': '坏规则', 'values': []}]},
            {'source_statistics': 'false'}, {'source_statistics': 0},
            {'accept_rules': [{'name': '同名', 'values': ['A']},
                              {'name': '同名', 'values': ['B']}]},
        ]
        for invalid in invalid_fields:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    C.ApprovalConfig(**invalid)
                with self.assertRaises(ValidationError):
                    C.ApprovalConfig(group_rules={'1001': invalid})
        self.assertIsNone(C.ApprovalConfig(source_statistics=None).source_statistics)
        self.assertIsNone(C.ApprovalConfig(group_rules={
            '1001': {'source_statistics': None},
        }).group_rules['1001']['source_statistics'])

    def test_approval_group_inheritance_preserves_omission_and_explicit_empty(self):
        config = C.ApprovalConfig(
            approval_mode='rules', source_statistics=True,
            accept_rules=[{'name': '全局答案', 'values': ['Alpha'], 'source': '渠道'}],
            accept_categories=['旧来源:全局'], reject_key=['拒绝'], legacy_global={'keep': True},
            group_rules={
                '1001': {'accept_categories': [], 'legacy_group': {'keep': [1]}},
                '1002': {'approval_mode': 'manual', 'accept_rules': [],
                         'reject_key': [], 'source_statistics': False},
                '1003': {'approval_mode': 'legacy'},
            },
        )
        self.assertTrue(callable(getattr(config, 'for_group', None)), '尚未实现分群审批继承')
        self.assertEqual(config.group_rules['1001'], {
            'accept_categories': [], 'legacy_group': {'keep': [1]},
        })
        inherited = config.for_group(1001)
        self.assertEqual(inherited['approval_mode'], 'rules')
        self.assertTrue(inherited['source_statistics'])
        self.assertEqual(inherited['accept_categories'], [])
        self.assertEqual(inherited['reject_key'], ['拒绝'])
        self.assertEqual(inherited['legacy_global'], {'keep': True})
        self.assertEqual(inherited['legacy_group'], {'keep': [1]})
        self.assertIsInstance(inherited['accept_rules'][0], dict)
        self.assertEqual(inherited['accept_rules'][0]['name'], '全局答案')
        group = config.for_group('1002')
        self.assertEqual(group['approval_mode'], 'manual')
        self.assertEqual(group['accept_rules'], [])
        self.assertEqual(group['reject_key'], [])
        self.assertFalse(group['source_statistics'])
        self.assertEqual(config.for_group(1003)['approval_mode'], 'legacy')
        self.assertEqual(config.for_group(1004)['approval_mode'], 'rules')
        inherited['accept_rules'][0]['values'].append('外部修改')
        inherited['legacy_group']['keep'].append(2)
        self.assertEqual(config.for_group(1001)['accept_rules'][0]['values'], ['Alpha'])
        self.assertEqual(config.for_group(1001)['legacy_group']['keep'], [1])

    def test_group_accept_rules_are_plain_dicts_without_sibling_defaults(self):
        config = C.ApprovalConfig(group_rules={'1001': {
            'accept_rules': [{'name': '群答案', 'values': ['Beta']}], 'legacy_rule': 1,
        }})
        rule = config.group_rules['1001']
        self.assertEqual(set(rule), {'accept_rules', 'legacy_rule'})
        self.assertIsInstance(rule['accept_rules'][0], dict)
        self.assertEqual(rule['accept_rules'][0].get('match'), 'exact')
        self.assertEqual(rule['accept_rules'][0].get('case_sensitive'), False)

    def test_source_statistics_defaults_follow_effective_approval_mode(self):
        config = C.ApprovalConfig(divide_group={
            'block_method': 'whitelist', 'control_list': [1001, 1002, 1003, 1004, 1005, 1006],
            'disabled_statistics': [1006],
        }, group_rules={
            '1002': {'approval_mode': 'rules'},
            '1003': {'approval_mode': 'manual'},
            '1004': {'approval_mode': 'rules', 'source_statistics': True},
            '1005': {'source_statistics': False},
            '1006': {'source_statistics': True},
        })
        self.assertTrue(callable(getattr(config, 'source_enabled', None)), '尚未拆分来源统计开关')
        for gid, expected in [(1001, True), (1002, False), (1003, False),
                              (1004, True), (1005, False), (1006, False), (1007, False)]:
            with self.subTest(gid=gid):
                self.assertEqual(config.source_enabled(gid), expected)
                self.assertEqual(config.chart_enabled(gid), expected)
        explicit = C.ApprovalConfig(approval_mode='manual', source_statistics=True,
                                   divide_group={'block_method': 'blacklist'},
                                   group_rules={'1001': {'source_statistics': False},
                                                '1002': {'source_statistics': None}})
        self.assertFalse(explicit.source_enabled(1001))
        self.assertFalse(explicit.source_enabled(1002))
        self.assertTrue(explicit.source_enabled(1003))

    def test_repository_updates_only_one_approval_group(self):
        repository = C.ConfigRepository(self.path)
        self.assertTrue(callable(getattr(repository, 'update_approval_group', None)),
                        '尚未实现原子更新单群审批配置')
        repository.save(C.GuardSettings(approval={
            'approval_mode': 'rules', 'legacy_global': [1],
            'accept_rules': [{'name': '全局', 'values': ['A']}],
            'group_rules': {
                '1001': {'accept_categories': ['旧:词'], 'legacy_group': {'keep': True},
                         'source_statistics': False},
                '1002': {'approval_mode': 'manual', 'reject_key': ['拒绝']},
            },
        }))
        repository.update_approval_group(1001, approval_mode='rules', accept_rules=[
            {'name': '新答案', 'values': ['B']},
        ])
        repository.update_approval_group('1003', approval_mode='manual')
        result = C.ConfigRepository(self.path).load().approval
        self.assertEqual(result.group_rules['1001']['accept_categories'], ['旧:词'])
        self.assertEqual(result.group_rules['1001']['legacy_group'], {'keep': True})
        self.assertFalse(result.group_rules['1001']['source_statistics'])
        self.assertEqual(result.for_group(1001)['accept_rules'][0]['name'], '新答案')
        self.assertEqual(result.group_rules['1002'], {
            'approval_mode': 'manual', 'reject_key': ['拒绝'],
        })
        self.assertEqual(result.group_rules['1003'], {'approval_mode': 'manual'})
        self.assertEqual(result.accept_rules[0].name, '全局')
        self.assertEqual(result.legacy_global, [1])

    def test_invalid_approval_update_preserves_disk_and_memory(self):
        repository = C.ConfigRepository(self.path)
        self.assertTrue(callable(getattr(repository, 'update_approval_group', None)),
                        '尚未实现原子更新单群审批配置')
        repository.save(C.GuardSettings(approval={'group_rules': {
            '1001': {'approval_mode': 'manual', 'legacy_group': [1]},
        }}))
        before = self.path.read_bytes()
        memory = repository.settings.model_dump()
        for gid, changes in [
            (1001, {'approval_mode': 'invalid'}), (1001, {'source_statistics': 'false'}),
            (1002, {'accept_rules': [{'name': '坏规则', 'values': []}]}),
            (0, {'approval_mode': 'manual'}),
        ]:
            with self.subTest(gid=gid, changes=changes):
                with self.assertRaises(ValueError):
                    repository.update_approval_group(gid, **changes)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(repository.settings.model_dump(), memory)

    def test_approval_update_disk_failure_is_atomic(self):
        repository = C.ConfigRepository(self.path)
        self.assertTrue(callable(getattr(repository, 'update_approval_group', None)),
                        '尚未实现原子更新单群审批配置')
        repository.save(C.GuardSettings(approval={'group_rules': {
            '1001': {'approval_mode': 'legacy', 'legacy_group': '保留'},
        }}))
        before = self.path.read_bytes()
        memory = repository.settings.model_dump()
        for method in ('replace', 'fsync'):
            with self.subTest(method=method):
                with mock.patch.object(S.os, method, side_effect=OSError('fixture disk failure')):
                    with self.assertRaises(OSError):
                        repository.update_approval_group(1001, approval_mode='manual')
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(repository.settings.model_dump(), memory)
                self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['config.json'])

    def test_templates_and_legacy_keys_are_not_dropped(self):
        config = C.GuardSettings.model_validate({
            'legacy_top': {'preserve': True},
            'approval': {'notice': {'custom': ['private:2001']},
                         'msg': {'custom': ['1001:链接：https://example.test/a:b']},
                         'legacy_approval': [1, 2]},
            'verification': {'legacy_global': {'x': 1}, 'group_configs': [{
                'group_id': 1001, '__template_key': 'default_config',
                'legacy_group': {'nested': ['值']}, 'error_verification': '群消息',
            }]},
        })
        payload = config.model_dump()
        self.assertEqual(payload['legacy_top'], {'preserve': True})
        self.assertEqual(payload['approval']['legacy_approval'], [1, 2])
        self.assertEqual(payload['verification']['legacy_global'], {'x': 1})
        group = payload['verification']['group_configs'][0]
        self.assertEqual(group['group_id'], '1001')
        self.assertEqual(group['__template_key'], 'default_config')
        self.assertEqual(group['legacy_group'], {'nested': ['值']})
        self.assertEqual(config.verification.for_group(1001)['error_verification'], '群消息')
        self.assertEqual(config.approval.msg['custom'][0], '1001:链接：https://example.test/a:b')

    def test_group_inheritance_keeps_false_zero_and_overrides(self):
        verification = C.VerificationConfig(
            verification_timeout=420, max_wrong_answers=7, kick_delay=8,
            enable_combined_verify=True, enable_level_verify=True, min_qq_level=30,
            verify_delay=6, recall_unverified_messages=True, warning_time=80,
            group_configs=[{'group_id': 1001, 'enabled': True,
                            'verification_timeout': None, 'max_wrong_answers': 2,
                            'enable_combined_verify': False, 'enable_level_verify': False,
                            'min_qq_level': 0, 'verify_delay': 0,
                            'recall_unverified_messages': False, 'warning_time': 0}],
        )
        group = verification.for_group('1001')
        self.assertTrue(group['enabled'])
        self.assertEqual(group['verification_timeout'], 420)
        self.assertEqual(group['max_wrong_answers'], 2)
        self.assertEqual(group['kick_delay'], 8)
        self.assertFalse(group['enable_combined_verify'])
        self.assertFalse(group['enable_level_verify'])
        self.assertEqual(group['min_qq_level'], 0)
        self.assertEqual(group['verify_delay'], 0)
        self.assertFalse(group['recall_unverified_messages'])
        self.assertEqual(group['warning_time'], 0)
        self.assertFalse(verification.for_group('1002')['enabled'])
        group['new_member_prompt'] = '外部修改'
        self.assertNotEqual(verification.for_group(1001)['new_member_prompt'], '外部修改')

    def test_secret_is_masked_but_saved_as_local_plaintext(self):
        settings = C.GuardSettings(verification={'api_key': 'fixture-secret-only'})
        self.assertIsInstance(settings.verification.api_key, SecretStr)
        self.assertNotIn('fixture-secret-only', repr(settings))
        self.assertNotIn('fixture-secret-only', str(settings))
        self.assertNotIn('api_key', settings.verification.for_group(1001))
        repository = C.ConfigRepository(self.path)
        repository.save(settings)
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(raw['verification']['api_key'], 'fixture-secret-only')
        self.assertEqual(C.ConfigRepository(self.path).load().verification.api_key.get_secret_value(),
                         'fixture-secret-only')

    def test_missing_config_returns_defaults_without_creating_file(self):
        repository = C.ConfigRepository(self.path)
        self.assertFalse(repository.load().enabled)
        self.assertFalse(self.path.exists())

    def test_bom_config_load_and_unknown_fields_roundtrip(self):
        self.path.write_text(json.dumps({
            'self_ids': [3001], 'legacy_top': '保留',
            'verification': {'api_key': 'fixture-key', 'group_configs': [{
                'group_id': 1001, 'enabled': True, '__template_key': 'default_config',
                'legacy_option': {'a': 1},
            }]},
        }, ensure_ascii=False), encoding='utf-8-sig')
        repository = C.ConfigRepository(self.path)
        settings = repository.load()
        self.assertEqual(settings.self_ids, ['3001'])
        repository.save(settings)
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(raw['legacy_top'], '保留')
        self.assertEqual(raw['verification']['group_configs'][0]['legacy_option'], {'a': 1})
        self.assertEqual(raw['verification']['group_configs'][0]['__template_key'], 'default_config')

    def test_corrupt_config_never_overwrites_file_or_loaded_memory(self):
        repository = C.ConfigRepository(self.path)
        repository.save(C.GuardSettings(enabled=True))
        damaged = b'\xef\xbb\xbf{"enabled":'
        self.path.write_bytes(damaged)
        with self.assertRaises(ValueError):
            repository.load()
        self.assertTrue(repository.settings.enabled)
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_invalid_config_does_not_replace_memory(self):
        repository = C.ConfigRepository(self.path)
        repository.save(C.GuardSettings(enabled=True))
        damaged = '{"verification":{"group_configs":[{"group_id":1001,"verification_timeout":-1}]}}'
        self.path.write_text(damaged, encoding='utf-8')
        with self.assertRaises(ValidationError):
            repository.load()
        self.assertTrue(repository.settings.enabled)
        self.assertEqual(self.path.read_text(encoding='utf-8'), damaged)

    def test_repository_updates_group_without_losing_legacy_keys(self):
        repository = C.ConfigRepository(self.path)
        repository.save(C.GuardSettings(verification={'group_configs': [{
            'group_id': 1001, 'enabled': False, '__template_key': 'default_config',
            'legacy_option': '原值',
        }]}))
        repository.set_enabled(True)
        repository.update_group(1001, enabled=True, verify_delay=0)
        repository.update_group('1002', max_wrong_answers=3)
        settings = C.ConfigRepository(self.path).load()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.verification.for_group(1001)['enabled'])
        self.assertEqual(settings.verification.for_group(1001)['verify_delay'], 0)
        self.assertEqual(settings.verification.for_group(1001)['legacy_option'], '原值')
        self.assertFalse(settings.verification.for_group(1002)['enabled'])
        self.assertEqual(len(settings.verification.group_configs), 2)
        with self.assertRaises(ValueError):
            repository.update_group(1001, group_id='1003')

    def test_repository_disk_error_preserves_memory_disk_and_cleans_temp(self):
        repository = C.ConfigRepository(self.path)
        repository.save(C.GuardSettings())
        before = self.path.read_bytes()
        with mock.patch.object(S.os, 'replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                repository.set_enabled(True)
            with self.assertRaises(OSError):
                repository.update_group(1001, enabled=True)
        self.assertFalse(repository.settings.enabled)
        self.assertEqual(repository.settings.verification.group_configs, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['config.json'])

    def test_repository_saved_snapshot_is_detached_from_caller(self):
        repository = C.ConfigRepository(self.path)
        candidate = C.GuardSettings(approval={'accept_categories': ['来源:测试']})
        repository.save(candidate)
        candidate.approval.accept_categories.append('外部:修改')
        self.assertEqual(repository.settings.approval.accept_categories, ['来源:测试'])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(S, '群管状态模块尚未实现')
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='group-guard-data-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.store = S.StateStore(self.path)

    def test_defaults_are_isolated_and_missing_load_is_read_only(self):
        expected = {'records': {}, 'sessions': {}, 'bypass': {}, 'requests': {}, 'notices': {}}
        self.assertEqual(self.store.load(), expected)
        self.assertFalse(self.path.exists())
        self.store.data['records']['1001'] = {}
        self.assertEqual(S.StateStore(self.path).data, expected)

    def test_bom_state_load_preserves_records_and_unknown_keys(self):
        records = {'1001': {'2001': {'category': '来源', 'accept_time': '旧时间', 'legacy': 1}}}
        self.path.write_text(json.dumps({'records': records, 'legacy_state': [1]}), encoding='utf-8-sig')
        result = self.store.load()
        self.assertEqual(result['records'], records)
        self.assertEqual(result['sessions'], {})
        self.assertEqual(result['legacy_state'], [1])
        self.store.save()
        self.assertEqual(S.StateStore(self.path).load(), self.store.data)

    def test_corrupt_or_invalid_state_preserves_memory_and_disk(self):
        self.store.transaction(lambda data: data['bypass'].update({'3001:1001:2001': True}))
        for invalid in ['{', '[]', '{"sessions": []}']:
            with self.subTest(invalid=invalid):
                self.path.write_text(invalid, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.store.load()
                self.assertTrue(self.store.data['bypass']['3001:1001:2001'])
                self.assertEqual(self.path.read_text(encoding='utf-8'), invalid)

    def test_transaction_commits_after_write_and_returns_mutator_result(self):
        def mutate(data):
            data['sessions']['3001:1001:2001'] = {'token': 'fixture-token'}
            return '完成'
        self.assertEqual(self.store.transaction(mutate), '完成')
        self.assertEqual(S.StateStore(self.path).load(), self.store.data)

    def test_failed_mutator_has_no_memory_or_disk_effect(self):
        self.store.save()
        before = self.path.read_bytes()
        def mutate(data):
            data['records']['1001'] = {'2001': {'category': '未提交'}}
            raise RuntimeError('fixture mutation failure')
        with self.assertRaises(RuntimeError):
            self.store.transaction(mutate)
        self.assertEqual(self.store.data['records'], {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_replace_or_fsync_cannot_commit_transaction(self):
        self.store.save()
        before = self.path.read_bytes()
        for method in ['replace', 'fsync']:
            with self.subTest(method=method):
                with mock.patch.object(S.os, method, side_effect=OSError('fixture disk failure')):
                    with self.assertRaises(OSError):
                        self.store.transaction(lambda data: data['bypass'].update({'x': True}))
                self.assertEqual(self.store.data['bypass'], {})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['state.json'])

    def test_non_json_mutation_cannot_change_memory(self):
        with self.assertRaises((TypeError, ValueError)):
            self.store.transaction(lambda data: data['records'].update({'invalid': {1, 2}}))
        self.assertEqual(self.store.data['records'], {})
        self.assertFalse(self.path.exists())

    def test_notice_history_is_bounded_without_touching_other_tables(self):
        count = S.NOTICE_LIMIT + 3
        def mutate(data):
            data['notices'].update({f'n{i}': i for i in range(count)})
            data['requests']['retained'] = True
        self.store.transaction(mutate)
        self.assertEqual(len(self.store.data['notices']), S.NOTICE_LIMIT)
        self.assertNotIn('n0', self.store.data['notices'])
        self.assertIn(f'n{count - 1}', self.store.data['notices'])
        self.assertTrue(self.store.data['requests']['retained'])
        self.assertEqual(S.StateStore(self.path).load(), self.store.data)


if __name__ == '__main__':
    unittest.main()
