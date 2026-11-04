"""只使用合成 AstrBot 数据验证迁移，不访问真实业务文件。"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'src/nonebot_plugin_group_guard'
PACKAGE = '_group_guard_migration_tests'
SCRATCH = Path(tempfile.gettempdir())


def load_guard(name):
    if not (PLUGIN / f'{name}.py').is_file():
        return None
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PLUGIN)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f'{PACKAGE}.{name}')


M = load_guard('migration')
C = load_guard('config')
S = load_guard('store')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(M, '群管迁移模块尚未实现')
        self.assertIsNotNone(C, '群管配置模块尚未实现')
        self.assertIsNotNone(S, '群管状态模块尚未实现')
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='group-guard-migration-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'astrbot'
        self.output = Path(self.temp.name) / 'target'
        self.approval = {
            'font': 'font.ttf', 'bg_img': 'selected.png', 'delay': 0.7,
            'accept_categories': ['类别甲:甲,AAA', '类别乙:乙', '类别丙:丙', '类别丁:丁', '类别戊:戊'],
            'reject_key': ['拒绝甲', '拒绝乙', '拒绝丙', '拒绝丁', '拒绝戊', '拒绝己', '拒绝庚'],
            'divide_group': {'block_method': 'whitelist', 'control_list': [1001, '1002'],
                             'disabled_statistics': ['1002'], 'legacy_scope': True},
            'notice': {'accept_notice': ['origin', 'default:FriendMessage:2001'],
                       'custom_notice': ['private:2002']},
            'msg': {'welcome_msg': ['default:欢迎 %user_name%', '1001:链接：https://example.test/a:b'],
                    'reject_reason': ['default:关键词 %key%'], 'custom_message': ['default:原值']},
            'legacy_approval': {'保留': 1},
        }
        self.verification = {
            'verification_timeout': 420, 'kick_delay': 8, 'max_wrong_answers': 7,
            'new_member_prompt': '入群 {question}', 'welcome_message': '完成 {at_user}',
            'wrong_answer_prompt': '答错 {question}', 'error_verification': '尚未验证',
            'failure_message': '超时 {countdown}', 'kick_message': '踢出',
            'too_many_wrong_message': '错误 {count}', 'api_base_url': 'https://verify.example.test',
            'api_key': 'fixture-migration-secret', 'enable_combined_verify': True,
            'enable_level_verify': True, 'min_qq_level': 30, 'verify_delay': 0,
            'legacy_global': {'保留': 2},
            'group_configs': [
                {'group_id': 1001, 'enabled': True, 'verification_timeout': 300,
                 'recall_unverified_messages': True, '__template_key': 'default_config',
                 'legacy_group_option': {'enabled': '原值'}},
                {'group_id': '1002', 'enabled': False, 'min_qq_level': 0},
            ],
        }
        self.records = {'1001': {'2001': {'category': '类别甲', 'accept_time': '旧时间',
                                         'accept_reason': '手动同意', 'legacy_record': [1]}}}
        self.write_json(self.root / 'data/config/astrbot_plugin_joinmanager_config.json', self.approval)
        self.write_json(self.root / 'data/config/astrbot_plugin_group_combined_captcha_verify_config.json',
                        self.verification)
        self.write_json(self.root / 'data/plugin_data/astrbot_plugin_joinmanager/join_records.json', self.records)
        self.math_path = self.root / 'data/config/astrbot_plugin_group_verification_pro_config.json'
        self.math_path.write_text('不是合法 JSON；独立数学 PRO 不应读取', encoding='utf-8')
        self.assets = self.root / 'data/plugins/astrbot_plugin_joinmanager/assets'
        self.assets.mkdir(parents=True)
        (self.assets / 'font.ttf').write_bytes(b'fixture-font')
        (self.assets / 'selected.png').write_bytes(b'fixture-selected-background')
        (self.assets / 'unused.txt').write_text('不应复制', encoding='utf-8')
        (self.assets / 'background/nested').mkdir(parents=True)
        (self.assets / 'background/a.png').write_bytes(b'fixture-bg-a')
        (self.assets / 'background/nested/b.png').write_bytes(b'fixture-bg-b')

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8-sig')

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_migration_preserves_legacy_values_with_safe_global_switch(self):
        before = self.snapshot()
        summary = M.migrate(self.root, self.output)
        settings = C.ConfigRepository(self.output / 'config.json').load()
        state = S.StateStore(self.output / 'state.json').load()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.self_ids, [])
        self.assertEqual(settings.approval.accept_categories, self.approval['accept_categories'])
        self.assertEqual(settings.approval.reject_key, self.approval['reject_key'])
        self.assertEqual(settings.approval.divide_group, self.approval['divide_group'])
        self.assertEqual(settings.approval.notice, self.approval['notice'])
        self.assertEqual(settings.approval.msg, self.approval['msg'])
        self.assertEqual(settings.approval.model_dump()['legacy_approval'], {'保留': 1})
        payload = settings.verification.model_dump()
        for key, value in self.verification.items():
            if key not in {'api_key', 'group_configs'}:
                self.assertEqual(payload[key], value, key)
        self.assertEqual(settings.verification.api_key.get_secret_value(), 'fixture-migration-secret')
        self.assertEqual(payload['group_configs'][0]['__template_key'], 'default_config')
        self.assertEqual(payload['group_configs'][0]['legacy_group_option'], {'enabled': '原值'})
        self.assertTrue(settings.verification.for_group(1001)['enabled'])
        self.assertFalse(settings.verification.for_group(1002)['enabled'])
        self.assertFalse(settings.verification.for_group(1003)['enabled'])
        self.assertEqual(state['records'], self.records)
        for key in ['sessions', 'bypass', 'requests', 'notices']:
            self.assertEqual(state[key], {})
        self.assertEqual(summary['accept_categories'], 5)
        self.assertEqual(summary['reject_keywords'], 7)
        self.assertEqual(summary['verification_groups'], 2)
        self.assertEqual(summary['record_groups'], 1)
        self.assertEqual(summary['records'], 1)
        safe_summary = json.dumps(summary, ensure_ascii=False)
        for forbidden in ['fixture-migration-secret', '1001', '1002', '2001', str(self.root)]:
            self.assertNotIn(forbidden, safe_summary)
        self.assertEqual(self.snapshot(), before)

    def test_only_selected_font_background_and_background_directory_copied(self):
        summary = M.migrate(self.root, self.output)
        chosen = self.output / 'assets'
        self.assertEqual(sorted(str(p.relative_to(chosen)).replace('\\', '/')
                                for p in chosen.rglob('*') if p.is_file()),
                         ['background/a.png', 'background/nested/b.png', 'font.ttf', 'selected.png'])
        approval = C.ConfigRepository(self.output / 'config.json').load().approval
        self.assertEqual(Path(approval.assets_dir), chosen.resolve())
        self.assertEqual(approval.font, 'font.ttf')
        self.assertEqual(approval.bg_img, 'selected.png')
        self.assertEqual(summary['assets_mode'], 'copied')
        self.assertEqual(summary['background_files'], 2)
        self.assertEqual(summary['background_bytes'], len(b'fixture-bg-a') + len(b'fixture-bg-b'))
        self.assertEqual(summary['assets_copied'], 4)

    def test_large_background_uses_original_assets_without_copying(self):
        with mock.patch.object(M, 'MAX_BACKGROUND_BYTES', 1):
            summary = M.migrate(self.root, self.output)
        approval = C.ConfigRepository(self.output / 'config.json').load().approval
        self.assertEqual(summary['assets_mode'], 'referenced')
        self.assertEqual(summary['assets_copied'], 0)
        self.assertEqual(summary['background_files'], 2)
        self.assertEqual(Path(approval.assets_dir), self.assets.resolve())
        self.assertFalse((self.output / 'assets').exists())

    def test_background_directory_symlink_is_referenced_not_copied(self):
        import shutil
        external = self.root.parent / 'existing-image-archive'
        external.mkdir()
        (external / 'one.png').write_bytes(b'archive-image')
        shutil.rmtree(self.assets / 'background')
        try:
            (self.assets / 'background').symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('当前系统不允许创建目录软链接')
        summary = M.migrate(self.root, self.output)
        self.assertEqual(summary['assets_mode'], 'referenced')
        self.assertFalse((self.output / 'assets').exists())
        self.assertEqual((external / 'one.png').read_bytes(), b'archive-image')

    def test_no_copy_assets_keeps_existing_source_path(self):
        summary = M.migrate(self.root, self.output, copy_assets=False)
        self.assertEqual(summary['assets_mode'], 'referenced')
        self.assertEqual(summary['assets_copied'], 0)
        settings = C.ConfigRepository(self.output / 'config.json').load()
        self.assertEqual(Path(settings.approval.assets_dir), self.assets.resolve())
        self.assertFalse((self.output / 'assets').exists())

    def test_either_existing_json_target_is_refused_without_any_write(self):
        for filename in ['config.json', 'state.json']:
            with self.subTest(filename=filename):
                output = self.output / filename.replace('.', '_')
                output.mkdir(parents=True)
                (output / filename).write_text('既有目标', encoding='utf-8')
                before = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
                with self.assertRaises(FileExistsError):
                    M.migrate(self.root, output)
                after = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
                self.assertEqual(after, before)
                self.assertEqual(sorted(p.name for p in output.iterdir()), [filename])

    def test_existing_assets_are_not_overwritten(self):
        (self.output / 'assets').mkdir(parents=True)
        marker = self.output / 'assets/font.ttf'
        marker.write_bytes(b'existing-business-font')
        with self.assertRaises(FileExistsError):
            M.migrate(self.root, self.output)
        self.assertEqual(marker.read_bytes(), b'existing-business-font')
        self.assertFalse((self.output / 'config.json').exists())
        self.assertFalse((self.output / 'state.json').exists())

    def test_invalid_or_missing_source_fails_without_partial_output(self):
        config_path = self.root / 'data/config/astrbot_plugin_group_combined_captcha_verify_config.json'
        config_path.write_text('{', encoding='utf-8')
        with self.assertRaises(ValueError):
            M.migrate(self.root, self.output)
        self.assertFalse(self.output.exists())
        config_path.unlink()
        with self.assertRaises(FileNotFoundError):
            M.migrate(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_join_records_produces_empty_records(self):
        (self.root / 'data/plugin_data/astrbot_plugin_joinmanager/join_records.json').unlink()
        summary = M.migrate(self.root, self.output)
        self.assertEqual(summary['records'], 0)
        self.assertEqual(S.StateStore(self.output / 'state.json').load()['records'], {})

    def test_invalid_records_shape_is_rejected_before_writing(self):
        path = self.root / 'data/plugin_data/astrbot_plugin_joinmanager/join_records.json'
        for invalid in [[], {'1001': []}, {'1001': {'2001': 'not-details'}}]:
            with self.subTest(invalid=invalid):
                self.write_json(path, invalid)
                with self.assertRaises(ValueError):
                    M.migrate(self.root, self.output)
                self.assertFalse(self.output.exists())

    def test_output_cannot_write_into_read_only_astrbot_tree(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            M.migrate(self.root, self.root / 'data/accidental-migration-output')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / 'data/accidental-migration-output').exists())

    def test_selected_assets_cannot_escape_source_tree(self):
        self.approval['font'] = '../outside.ttf'
        self.write_json(self.root / 'data/config/astrbot_plugin_joinmanager_config.json', self.approval)
        with self.assertRaises(ValueError):
            M.migrate(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_target_created_during_migration_is_not_overwritten(self):
        original_open = os.open
        def competing_writer(path, flags, *args, **kwargs):
            if Path(path) == self.output / 'state.json' and flags & os.O_EXCL:
                Path(path).write_bytes(b'concurrent-business-state')
            return original_open(path, flags, *args, **kwargs)
        with mock.patch.object(S.os, 'open', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                M.migrate(self.root, self.output)
        self.assertEqual((self.output / 'state.json').read_bytes(), b'concurrent-business-state')
        self.assertFalse((self.output / 'config.json').exists())
        self.assertFalse((self.output / 'assets').exists())

    def test_second_commit_disk_failure_rolls_back_new_targets(self):
        original_replace = os.replace
        def fail_state(source, destination):
            if Path(destination).name == 'state.json' and Path(destination).parent == self.output:
                raise OSError('fixture state commit failure')
            return original_replace(source, destination)
        with mock.patch.object(S.os, 'replace', side_effect=fail_state):
            with self.assertRaises(OSError):
                M.migrate(self.root, self.output)
        self.assertFalse((self.output / 'config.json').exists())
        self.assertFalse((self.output / 'state.json').exists())
        self.assertFalse((self.output / 'assets').exists())
        self.assertTrue(not self.output.exists() or not any(self.output.iterdir()))

    def test_cli_help_and_fixture_migration(self):
        script = str(PLUGIN / 'migration.py')
        result = subprocess.run([sys.executable, script, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--astrbot-root', result.stdout)
        self.assertIn('--output-dir', result.stdout)
        self.assertIn('--no-copy-assets', result.stdout)
        result = subprocess.run([
            sys.executable, script, '--astrbot-root', str(self.root),
            '--output-dir', str(self.output), '--no-copy-assets',
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fixture-migration-secret', result.stdout + result.stderr)
        self.assertNotIn('1001', result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['records'], 1)
        self.assertFalse(C.ConfigRepository(self.output / 'config.json').load().enabled)
        result = subprocess.run([
            sys.executable, script, '--astrbot-root', str(self.root),
            '--output-dir', str(self.output),
        ], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('fixture-migration-secret', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
