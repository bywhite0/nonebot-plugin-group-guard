"""独立安装路径与项目边界回归。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StandaloneTests(unittest.TestCase):
    def test_installed_style_import_uses_application_data_not_package_ancestors(self):
        code = r'''
from pathlib import Path
import nonebot
nonebot.init(_env_file=None, driver="~fastapi")
plugin = nonebot.load_plugin("nonebot_plugin_group_guard")
assert plugin is not None
module = plugin.module
expected = Path.cwd() / "data/group_guard"
assert module.repo.path == expected / "config.json", (module.repo.path, expected)
assert module.store.path == expected / "state.json"
assert not module.repo.settings.enabled
print("STANDALONE_DATA_PATH_OK")
'''
        with tempfile.TemporaryDirectory(prefix="group-guard-standalone-", dir=os.environ.get("TMPDIR")) as directory:
            env = {k: v for k, v in os.environ.items()
                   if k.upper() not in {"GROUP_GUARD_CONFIG_FILE", "GROUP_GUARD_STATE_FILE"}}
            env["PYTHONPATH"] = str(ROOT / "src")
            result = subprocess.run([sys.executable, "-c", code], cwd=directory, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=40, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STANDALONE_DATA_PATH_OK", result.stdout)

    def test_package_and_docs_contain_no_specific_application_paths(self):
        import re
        paths = list((ROOT / "src").rglob("*.py")) + list((ROOT / "src").rglob("*.md"))
        paths += list((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
        banned_name = "little" + "bot"
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(banned_name, text.casefold())
                self.assertNotRegex(text, re.compile(r"[CD]:[/\\]Users[/\\]"))
                self.assertNotIn("HOST_ROOT", text)


if __name__ == "__main__":
    unittest.main()
