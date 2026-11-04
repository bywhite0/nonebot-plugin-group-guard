"""真实 NoneBot matcher 分发 + 假 OneBot API 的端到端冒烟，不连接 QQ。"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get('TMPDIR') or Path(tempfile.gettempdir()))
SCRATCH.mkdir(parents=True, exist_ok=True)


async def main():
    with tempfile.TemporaryDirectory(prefix='guard-e2e-', dir=SCRATCH) as directory:
        root = Path(directory)
        import nonebot
        nonebot.init(_env_file=None, driver='~fastapi', command_start={'/'}, superusers={'700'},
                     group_guard_config_file=root / 'config.json', group_guard_state_file=root / 'state.json')
        from nonebot.adapters.onebot.v11 import (
            Adapter, Bot, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent,
            GroupMessageEvent, GroupRequestEvent, Message,
        )
        from nonebot.message import handle_event
        nonebot.get_driver().register_adapter(Adapter)
        sys.path.insert(0, str(ROOT / 'src'))
        plugin = nonebot.load_plugin('nonebot_plugin_group_guard')
        assert plugin is not None
        mod = plugin.module
        settings = mod.repo.settings.model_copy(deep=True)
        settings.enabled = True
        settings.approval.divide_group['control_list'] = ['100']
        settings.approval.accept_categories = ['GitHub:github']
        settings.approval.reject_key = ['广告']
        settings.approval.notice = {'accept_notice': ['origin'], 'reject_notice': ['origin'],
                                    'increase_notice': ['origin'], 'decrease_notice': []}
        settings.approval.delay = 0
        mod.repo.save(settings)
        mod.repo.update_group(100, enabled=True, verify_delay=0, enable_combined_verify=False)
        calls = []
        original = Bot.call_api

        async def call_api(self, api, **kw):
            calls.append((api, kw))
            if api == 'get_group_member_info':
                return {'role': 'admin' if kw['user_id'] == 900 else 'member',
                        'nickname': '离线测试成员', 'join_time': 100, 'card': ''}
            if api == 'get_stranger_info':
                return {'nickname': '离线测试成员', 'qqLevel': 1}
            return {'message_id': len(calls)}

        Bot.call_api = call_api
        try:
            bot = Bot(nonebot.get_adapter(Adapter), '900')
            def group_message(uid, text, role='member'):
                return GroupMessageEvent(time=1000, self_id=900, post_type='message',
                    message_type='group', sub_type='normal', message_id=11, user_id=uid,
                    group_id=100, message=Message(text), raw_message=text, font=0,
                    sender={'user_id': uid, 'nickname': 'tester', 'role': role})

            # 未配置群的申请不会产生任何消息或审批。
            ignored = GroupRequestEvent(time=1000,self_id=900,post_type='request',request_type='group',
                sub_type='add',group_id=888,user_id=200,comment='github',flag='ignored')
            await handle_event(bot, ignored)
            assert not calls
            request = GroupRequestEvent(time=1000,self_id=900,post_type='request',request_type='group',
                sub_type='add',group_id=100,user_id=200,comment='GitHub',flag='offline-only')
            await handle_event(bot, request)
            await handle_event(bot, request)
            approvals = [kw for api,kw in calls if api == 'set_group_add_request']
            assert len(approvals) == 1 and approvals[0]['approve'] is True
            await handle_event(bot, GroupIncreaseNoticeEvent(time=1001,self_id=900,post_type='notice',
                notice_type='group_increase',sub_type='approve',group_id=100,operator_id=900,user_id=200))
            session = mod.service.pending('900',100,200)
            assert session and session['method'] == 'math'
            await handle_event(bot, group_message(200,str(session['answer'])))
            assert mod.service.pending('900',100,200) is None
            assert 'GitHub' == mod.store.data['records']['100']['200']['category']
            assert not any(api == 'set_group_kick' for api,_ in calls)
            # 真实命令路由：成员改配置失败，管理改配置成功。
            await handle_event(bot, group_message(200,'/设置最低验证等级 25'))
            assert mod.repo.settings.verification.for_group(100)['min_qq_level'] == 20
            await handle_event(bot, group_message(201,'/设置最低验证等级 25', 'admin'))
            assert mod.repo.settings.verification.for_group(100)['min_qq_level'] == 25
            await handle_event(bot, group_message(201,'/入群统计','admin'))
            images = [seg for api,kw in calls if api == 'send_group_msg'
                      for seg in kw['message'] if seg.type == 'image']
            assert images and all(seg.data['file'].startswith('base64://') for seg in images)
            await handle_event(bot, GroupDecreaseNoticeEvent(time=1002,self_id=900,post_type='notice',
                notice_type='group_decrease',sub_type='leave',group_id=100,operator_id=200,user_id=200))
            assert '200' not in mod.store.data['records'].get('100',{})
            # 落盘回读当前精确目标。
            disk = json.loads((root/'state.json').read_text(encoding='utf-8'))
            assert disk == mod.store.data
            assert json.loads((root/'config.json').read_text(encoding='utf-8'))['enabled']
            # 规则模式只校验实际回答，不把问题文本或规则名当成来源。
            mod.repo.update_approval_group(100, approval_mode='rules', accept_rules=[
                {'name': '成员知识题', 'match': 'exact', 'values': ['花帆', '吟子']},
            ])
            before = len([kw for api, kw in calls if api == 'set_group_add_request'])
            for uid, comment, flag in [
                (300, '问题：请输入花帆\n答案：不知道', 'fixed-wrong'),
                (301, '问题：喜欢的成员\n答案：吟子', 'fixed-correct'),
            ]:
                await handle_event(bot, GroupRequestEvent(time=1003, self_id=900, post_type='request',
                    request_type='group', sub_type='add', group_id=100, user_id=uid,
                    comment=comment, flag=flag))
            assert len([kw for api, kw in calls if api == 'set_group_add_request']) == before + 1
            record = mod.store.data['records']['100']['301']
            assert record['source_category'] is None and record['approval_rule'] == '成员知识题'
            assert not mod.repo.settings.approval.chart_enabled(100)
            mod.repo.update_approval_group(100, approval_mode='manual')
            await handle_event(bot, GroupRequestEvent(time=1004, self_id=900, post_type='request',
                request_type='group', sub_type='add', group_id=100, user_id=302,
                comment='花帆', flag='manual-only'))
            assert len([kw for api, kw in calls if api == 'set_group_add_request']) == before + 1
            assert json.loads((root/'state.json').read_text(encoding='utf-8')) == mod.store.data
            print('GROUP_GUARD_E2E_OK', json.dumps({
                'matcher_events': 12, 'approval_calls': len([kw for api, kw in calls if api == 'set_group_add_request']),
                'image_segments':len(images), 'real_qq_calls':0,
                'source_removed_after_leave':True, 'disk_matches_memory':True,
            }, ensure_ascii=False))
        finally:
            Bot.call_api = original
            await mod.service.close()


if __name__ == '__main__':
    asyncio.run(main())
