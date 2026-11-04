"""离线迁移入口不能初始化 NoneBot 或加载业务插件。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_offline_help_without_initializing_nonebot(self):
        code = r'''
import sys
from nonebot_group_guard_migrate import main
sys.argv = ['group-guard-migrate', '--help']
try:
    main()
except SystemExit as error:
    assert error.code == 0
else:
    raise AssertionError('help should exit')
assert 'nonebot_plugin_group_guard' not in sys.modules
assert 'nonebot' not in sys.modules
print('OFFLINE_CLI_OK')
'''
        with tempfile.TemporaryDirectory(prefix="guard-cli-", dir=os.environ.get("TMPDIR")) as directory:
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            result = subprocess.run([sys.executable, "-c", code], cwd=directory, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--astrbot-root", result.stdout)
        self.assertIn("OFFLINE_CLI_OK", result.stdout)


    def test_offline_migration_from_installed_entry_preserves_safe_defaults(self):
        import json
        with tempfile.TemporaryDirectory(prefix="guard-cli-migrate-", dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            source = root / "AstrBot"
            configs = source / "data/config"
            configs.mkdir(parents=True)
            (configs / "astrbot_plugin_joinmanager_config.json").write_text(json.dumps({
                "accept_categories": ["开源社区:github"], "reject_key": ["禁止词"],
            }), encoding="utf-8")
            (configs / "astrbot_plugin_group_combined_captcha_verify_config.json").write_text("{}", encoding="utf-8")
            (source / "data/plugins/astrbot_plugin_joinmanager/assets").mkdir(parents=True)
            target = root / "output"
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            result = subprocess.run([sys.executable, "-m", "nonebot_group_guard_migrate",
                                     "--astrbot-root", str(source), "--output-dir", str(target)],
                                    cwd=root, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("NoneBot is initializing", result.stdout)
            config = json.loads((target / "config.json").read_text(encoding="utf-8"))
            self.assertFalse(config["enabled"])
            self.assertEqual(config["approval"]["accept_categories"], ["开源社区:github"])
            self.assertEqual(json.loads((target / "state.json").read_text(encoding="utf-8"))["records"], {})


if __name__ == "__main__":
    unittest.main()
