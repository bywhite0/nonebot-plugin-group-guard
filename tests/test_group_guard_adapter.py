"""真实 NoneBot 加载与权限入口检查，在子进程隔离全局 matcher。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'src/nonebot_plugin_group_guard/__init__.py'


class AdapterTests(unittest.TestCase):
    def test_load_and_permission_boundaries(self):
        self.assertTrue(ENTRY.exists(), 'NoneBot 群管入口尚未实现')
        code = r'''
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import nonebot
nonebot.init(_env_file=None, superusers={'700'}, driver='~fastapi', command_start={'/'})
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
nonebot.get_driver().register_adapter(Adapter)
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
p = nonebot.load_plugin('nonebot_plugin_group_guard')
assert p is not None
m = p.module
assert not m.repo.settings.enabled
assert len(p.matcher) >= 12

def event(uid, role='member', text='x'):
    return GroupMessageEvent(time=1000, self_id=900, post_type='message', message_type='group',
        sub_type='normal', message_id=10, user_id=uid, group_id=100, message=Message(text),
        raw_message=text, font=0, sender={'user_id':uid, 'role':role, 'nickname':'tester'})

class Matcher:
    def __init__(self): self.sent=[]
    async def finish(self, text):
        self.sent.append(text)
        raise FinishedException

bot=Bot(nonebot.get_adapter(Adapter), '900')
async def main():
    assert not await m.can_manage(bot, event(200))
    assert await m.can_manage(bot, event(201, 'admin'))
    assert await m.can_manage(bot, event(202, 'owner'))
    assert await m.can_manage(bot, event(700))
    assert not m.answer_rule(bot, event(200, text='42'))
    matcher=Matcher()
    for callback,args in [(m.master_switch,Message('开启')),(m.verification_settings,Message(''))]:
        try:
            if callback is m.master_switch:
                await callback(matcher,bot,event(201,'admin'),args)
            else:
                await callback(matcher,bot,event(200),args)
        except FinishedException:
            pass
        else:
            raise AssertionError('unauthorized handler did not stop')
    assert not m.repo.settings.enabled
    assert len(matcher.sent)==2
    assert all('权限' in str(x) or '只有' in str(x) for x in matcher.sent)
    assert m.extract_target(Message([MessageSegment.at(333)])) == 333
    assert m.extract_target(Message('333')) == 333
    assert m.extract_target(Message('not333')) is None
    assert m.extract_target(Message([MessageSegment.at(333),MessageSegment.at(334)])) is None
    async def invoke(callback, uid, role, args):
        try:
            await callback(matcher,bot,event(uid,role),Message(args))
        except FinishedException:
            pass
    await invoke(m.approval_switch,201,'admin','开启')
    assert m.repo.settings.approval.allows(100)
    await invoke(m.manage_keywords,201,'admin','允许添加 来源:测试词')
    assert m.repo.settings.approval.group_rules['100']['accept_categories'] == ['来源:测试词']
    await invoke(m.approval_switch,201,'admin','关闭')
    assert not m.repo.settings.approval.allows(100)
    await invoke(m.set_approval_mode,201,'admin','规则')
    assert m.repo.settings.approval.for_group(100)['approval_mode'] == 'rules'
    await invoke(m.manage_approval_rules,201,'admin','添加 {"name":"知识题","match":"exact","values":["花帆","吟子"]}')
    assert m.repo.settings.approval.for_group(100)['accept_rules'][0]['name'] == '知识题'
    await invoke(m.manage_keywords,201,'admin','拒绝添加 禁止词')
    assert m.repo.settings.approval.for_group(100)['approval_mode'] == 'rules'
    assert m.repo.settings.approval.for_group(100)['accept_rules'][0]['name'] == '知识题'
    assert '101' not in m.repo.settings.approval.group_rules
    before = m.repo.settings.model_dump()
    await invoke(m.set_approval_mode,200,'member','人工')
    assert m.repo.settings.model_dump() == before
    await invoke(m.manage_approval_rules,201,'admin','添加 {"name":"错误规则","match":"exact","values":[]}')
    assert len(m.repo.settings.approval.for_group(100)['accept_rules']) == 1
    await invoke(m.set_source_statistics,201,'admin','开启')
    assert m.repo.settings.approval.for_group(100)['source_statistics'] is True
    await invoke(m.set_approval_mode,201,'admin','兼容')
    await invoke(m.set_source_statistics,201,'admin','默认')
    m.repo.update_group(100, enabled=True, verify_delay=0, new_member_prompt='{at_user} 群级提示 {question}')
    class FakeBot:
        self_id='900'
        def __init__(self): self.calls=[]
        async def call_api(self,api,**kw):
            self.calls.append((api,kw))
            if api=='get_group_member_info':
                return {'role':'admin' if kw['user_id']==900 else 'member','nickname':'tester','join_time':5}
            return {'message_id':1}
    fake=FakeBot()
    m.repo.set_enabled(True)
    await m.service.handle_join(fake,SimpleNamespace(group_id=100,user_id=200,time=1000))
    assert any('群级提示' in str(kw['message']) for api,kw in fake.calls if api=='send_group_msg')
    assert hasattr(m.repo.settings.verification,'countdown_warning_prompt')
    await m.service.close()
asyncio.run(main())
print('GROUP_GUARD_ADAPTER_OK')
'''
        scratch = Path(os.environ.get('TMPDIR') or Path(tempfile.gettempdir()))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='group-guard-adapter-', dir=scratch) as td:
            env = dict(os.environ, GROUP_GUARD_CONFIG_FILE=str(Path(td) / 'config.json'), GROUP_GUARD_STATE_FILE=str(Path(td) / 'state.json'))
            result = subprocess.run([sys.executable, '-c', code, str(ROOT)], cwd=td, env=env, capture_output=True, text=True, encoding='utf-8', timeout=40, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('GROUP_GUARD_ADAPTER_OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
