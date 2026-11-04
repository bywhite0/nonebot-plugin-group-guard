"""假 Bot、可控时钟测试，不对 QQ 发起任何调用。"""
import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'src/nonebot_plugin_group_guard'
PKG = '_guard_service_unit'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PLUGIN)]
sys.modules[PKG] = pkg


def load_service():
    path = PLUGIN / 'service.py'
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(PKG + '.service', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 服务层单测仅捕获消息语义；真实消息段在独立集成测试校验。
message_stub = types.ModuleType(PKG + '.messages')
message_stub.to_message = lambda text, **kw: {'text': text, **kw}
sys.modules[message_stub.__name__] = message_stub
S = load_service()


class MemoryStore:
    def __init__(self):
        self.data = {k: {} for k in ('records', 'sessions', 'bypass', 'requests', 'notices')}
        self.fail = False

    def transaction(self, fn):
        candidate = copy.deepcopy(self.data)
        result = fn(candidate)
        if self.fail:
            raise OSError('disk full')
        self.data = candidate
        return result


class Approval:
    accept_categories = ['GitHub:github', 'B站:bilibili']
    reject_key = ['广告']
    notice = {'accept_notice': ['origin'], 'reject_notice': ['origin'], 'increase_notice': ['origin'], 'decrease_notice': ['origin']}
    msg = {'welcome_msg': ['default:欢迎 %user_name% 来源%category%'], 'increase_msg': ['default:欢迎人工审核成员'], 'decrease_msg': ['default:%user_name% 已离开'], 'reject_reason': ['default:不接受 %key%']}
    delay = 0
    font = ''
    bg_img = ''
    assets_dir = ''

    def allows(self, gid):
        return str(gid) in ('100', '101')

    def for_group(self, gid):
        return {'approval_mode': 'legacy', 'source_statistics': None,
                **getattr(self, 'group_rules', {}).get(str(gid), {})}

    def source_enabled(self, gid):
        config = self.for_group(gid)
        enabled = config.get('source_statistics')
        return self.allows(gid) and (enabled if enabled is not None else config['approval_mode'] == 'legacy')

    def chart_enabled(self, gid):
        return False


class Verification:
    api_base_url = 'https://verify.example.test'
    api_key = NS(get_secret_value=lambda: 'neutral-test-key')
    new_member_prompt = '{at_user} 请在 {timeout} 分钟内作答 {question}'
    welcome_message = '{at_user} 验证成功'
    wrong_answer_prompt = '{at_user} 答案错误，新问题 {question}'
    error_verification = '{at_user} 请先完成验证'
    countdown_warning_prompt = '{at_user} 验证即将超时'
    failure_message = '{at_user} 验证超时，{countdown} 秒后移出'
    kick_message = '{at_user} 已移出本群'
    too_many_wrong_message = '{at_user} 已答错 {count} 次'
    warning_time = 20
    kick_delay = 5
    verification_timeout = 100
    max_wrong_answers = 3

    def __init__(self):
        self.groups = {}

    def for_group(self, gid):
        return dict(enabled=str(gid) in ('100', '101'), verification_timeout=self.verification_timeout,
                    max_wrong_answers=self.max_wrong_answers, enable_combined_verify=False,
                    enable_level_verify=False, min_qq_level=20, verify_delay=0,
                    recall_unverified_messages=False, warning_time=self.warning_time,
                    kick_delay=self.kick_delay, **self.groups.get(str(gid), {}))


class Repo:
    def __init__(self):
        self.settings = NS(enabled=True, self_ids=[], approval=Approval(), verification=Verification())

    def update_group(self, gid, **changes):
        # dict 展开不能有重复键，在返回前合并。
        original = self.settings.verification.for_group
        current = original(gid)
        current.update(changes)
        self.settings.verification.for_group = lambda target: current.copy() if str(target) == str(gid) else original(target)


class FakeBackend:
    def __init__(self):
        self.check_result = True
        self.create_error = False
        self.check_error = False
        self.calls = []
        self.gate = None

    async def create(self, gid, uid):
        self.calls.append(('create', gid, uid))
        if self.create_error:
            raise RuntimeError('backend unavailable')
        return 'https://verify.example.test/v/ticket?lang=zh'

    async def check(self, gid, uid, code):
        self.calls.append(('check', gid, uid, code))
        if self.gate:
            await self.gate.wait()
        if self.check_error:
            raise RuntimeError('backend unavailable')
        return self.check_result

    async def close(self):
        pass


class FakeBot:
    self_id = '900'

    def __init__(self):
        self.calls = []
        self.roles = {}
        self.level = 0
        self.join_time = 10
        self.fail_send = False
        self.fail_member = False
        self.fail_approve = False

    async def call_api(self, api, **kw):
        self.calls.append((api, kw))
        if api == 'get_group_member_info':
            if self.fail_member:
                raise RuntimeError('not available')
            uid = kw['user_id']
            return {'nickname': '成员', 'card': '', 'role': self.roles.get(uid, 'admin' if str(uid) == self.self_id else 'member'), 'join_time': self.join_time}
        if api == 'get_stranger_info':
            return {'nickname': '成员', 'qqLevel': self.level}
        if api in ('send_group_msg', 'send_private_msg'):
            if self.fail_send:
                raise RuntimeError('send unavailable')
            return {'message_id': len(self.calls)}
        if api == 'set_group_add_request' and self.fail_approve:
            raise RuntimeError('approve failed')
        return {}

    def actions(self, api):
        return [kw for name, kw in self.calls if name == api]


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(S, '群管服务模块尚未实现')
        self.clock = 1000.0
        self.repo, self.store, self.bot = Repo(), MemoryStore(), FakeBot()
        self.backend = FakeBackend()
        self.service = S.GuardService(self.repo, self.store, backend_factory=lambda *a, **k: self.backend, now=lambda: self.clock)

    async def asyncTearDown(self):
        if hasattr(self, 'service'):
            await self.service.close()

    def event(self, gid=100, uid=200, **kw):
        return NS(group_id=gid, user_id=uid, sub_type=kw.pop('sub_type', 'approve'), time=kw.pop('time', 1000), **kw)

    def answer(self, text, gid=100, uid=200):
        return self.event(gid, uid, message_id=11, message=[{'type': 'text', 'data': {'text': text}}])

    async def join(self, gid=100, uid=200):
        await self.service.handle_join(self.bot, self.event(gid, uid))
        return self.service.pending(self.bot.self_id, gid, uid)

    async def test_unconfigured_groups_are_completely_silent(self):
        await self.service.handle_request(self.bot, self.event(888, comment='github', flag='req', sub_type='add'))
        await self.service.handle_join(self.bot, self.event(888))
        await self.service.handle_leave(self.bot, self.event(888, sub_type='leave'))
        self.assertEqual(self.bot.calls, [])

    async def test_master_switch_disables_all_event_actions(self):
        self.repo.settings.enabled = False
        await self.join()
        self.assertEqual(self.bot.calls, [])

    async def test_wrong_bot_is_silent(self):
        self.repo.settings.self_ids = ['901']
        await self.join()
        self.assertEqual(self.bot.calls, [])

    async def test_request_keyword_approval_and_deduplication(self):
        event = self.event(comment='GitHub', flag='req-1', sub_type='add')
        await asyncio.gather(self.service.handle_request(self.bot, event), self.service.handle_request(self.bot, event))
        self.assertEqual(len(self.bot.actions('set_group_add_request')), 1)
        self.assertTrue(self.bot.actions('set_group_add_request')[0]['approve'])
        self.assertEqual(self.store.data['records']['100']['200']['category'], 'GitHub')

    async def test_reject_beats_accept_and_no_record_is_created(self):
        await self.service.handle_request(self.bot, self.event(comment='github 广告', flag='req', sub_type='add'))
        self.assertFalse(self.bot.actions('set_group_add_request')[0]['approve'])
        self.assertEqual(self.store.data['records'], {})

    async def test_invite_and_unmatched_request_not_auto_processed(self):
        await self.service.handle_request(self.bot, self.event(comment='github', flag='req', sub_type='invite'))
        await self.service.handle_request(self.bot, self.event(comment='朋友', flag='req2', sub_type='add'))
        self.assertEqual(self.bot.actions('set_group_add_request'), [])

    async def test_failed_approval_not_counted(self):
        self.bot.fail_approve = True
        await self.service.handle_request(self.bot, self.event(comment='github', flag='req', sub_type='add'))
        self.assertEqual(self.store.data['records'], {})

    async def test_same_user_in_two_groups_is_isolated(self):
        a = await self.join()
        b = await self.join(101)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        await self.service.handle_answer(self.bot, self.answer(str(a['answer'])))
        self.assertIsNone(self.service.pending('900', 100, 200))
        self.assertIsNotNone(self.service.pending('900', 101, 200))

    async def test_duplicate_join_does_not_reset_deadline(self):
        a = await self.join()
        self.clock += 10
        b = await self.join()
        self.assertEqual(a['token'], b['token'])
        self.assertEqual(a['deadline'], b['deadline'])

    async def test_self_admin_and_bypass_are_exempt(self):
        await self.join(uid=900)
        self.bot.roles[201] = 'admin'
        await self.join(uid=201)
        await self.service.bypass(self.bot, 100, 202)
        await self.join(uid=202)
        for uid in (900, 201, 202):
            self.assertIsNone(self.service.pending('900', 100, uid))
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_qq_level_exemption(self):
        self.repo.update_group(100, enable_level_verify=True)
        self.bot.level = 20
        await self.join()
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_whole_plain_text_answers_only(self):
        session = await self.join()
        for text in ['hello 42', '[CQ:at,qq=42]', '42abc']:
            self.assertFalse(await self.service.handle_answer(self.bot, self.answer(text)))
        self.assertEqual(self.service.pending('900', 100, 200)['wrong_count'], 0)
        e = self.answer(str(session['answer']))
        e.message.insert(0, {'type': 'at', 'data': {'qq': '900'}})
        self.assertTrue(await self.service.handle_answer(self.bot, e))
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_mixed_image_and_numeric_text_is_not_an_answer(self):
        s = await self.join()
        e = self.answer(str(s['answer']))
        e.message.append({'type': 'image', 'data': {'file': 'not-real'}})
        self.assertFalse(await self.service.handle_answer(self.bot, e))
        self.assertIsNotNone(self.service.pending('900', 100, 200))

    async def test_wrong_answer_has_limit_and_regenerates(self):
        a = await self.join()
        wrong = str((int(a['answer']) + 1) % 101)
        await self.service.handle_answer(self.bot, self.answer(wrong))
        b = self.service.pending('900', 100, 200)
        self.assertEqual(b['wrong_count'], 1)
        self.assertNotEqual(a['token'], b['token'])
        for _ in range(2):
            b = self.service.pending('900', 100, 200)
            await self.service.handle_answer(self.bot, self.answer(str((int(b['answer']) + 1) % 101)))
        self.clock += 6
        await self.service.tick(self.bot)
        self.assertEqual(len(self.bot.actions('set_group_kick')), 1)
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_timeout_warning_grace_and_single_kick(self):
        await self.join()
        self.clock = 1081
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.clock = 1101
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.clock = 1107
        await self.service.tick(self.bot)
        await self.service.tick(self.bot)
        self.assertEqual(len(self.bot.actions('set_group_kick')), 1)
        self.assertFalse(self.bot.actions('set_group_kick')[0]['reject_add_request'])

    async def test_correct_answer_during_grace_prevents_kick(self):
        a = await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        await self.service.handle_answer(self.bot, self.answer(str(a['answer'])))
        self.clock = 1110
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_leave_rejoin_old_deadline_cannot_affect_new_session(self):
        a = await self.join()
        await self.service.handle_leave(self.bot, self.event(sub_type='leave'))
        self.clock = 1050
        self.bot.join_time = 20
        b = await self.join()
        self.assertNotEqual(a['token'], b['token'])
        self.clock = 1107
        await self.service.tick(self.bot)
        self.assertIsNotNone(self.service.pending('900', 100, 200))
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_bypass_and_disable_cancel_future_penalty(self):
        await self.join()
        await self.service.bypass(self.bot, 100, 200)
        await self.join(uid=201)
        await self.service.disable_group(self.bot, 100)
        self.clock = 9999
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_send_failure_leaves_manual_state_not_pending_penalty(self):
        self.bot.fail_send = True
        await self.join()
        self.clock = 9999
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_cannot_kick_when_bot_loses_admin(self):
        await self.join()
        self.bot.roles[900] = 'member'
        self.clock = 1101
        await self.service.tick(self.bot)
        self.clock = 1110
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_member_join_time_changed_before_kick_is_protected(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        self.bot.join_time = 30
        self.clock = 1110
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_restore_reissues_with_full_timeout(self):
        old = await self.join()
        self.clock = 9999
        await self.service.restore(self.bot)
        new = self.service.pending('900', 100, 200)
        self.assertNotEqual(old['token'], new['token'])
        self.assertGreaterEqual(new['deadline'], self.clock + 100)
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_web_create_fails_back_to_real_math_challenge(self):
        self.repo.update_group(100, enable_combined_verify=True)
        self.backend.create_error = True
        s = await self.join()
        self.assertEqual(s['method'], 'math')
        self.assertIsInstance(s['answer'], int)

    async def test_web_check_backend_error_is_not_wrong_answer(self):
        self.repo.update_group(100, enable_combined_verify=True)
        s = await self.join()
        self.assertEqual(s['method'], 'combined')
        self.backend.check_error = True
        await self.service.handle_answer(self.bot, self.answer('000042'))
        current = self.service.pending('900', 100, 200)
        self.assertEqual(current['wrong_count'], 0)
        self.assertEqual(current['method'], 'math')
        self.assertGreaterEqual(current['deadline'], self.clock + 100)

    async def test_web_verification_uses_plain_six_digit_code(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        await self.service.handle_answer(self.bot, self.answer('000042'))
        self.assertIn(('check', 100, 200, '000042'), self.backend.calls)
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_departure_removes_source_record(self):
        await self.join()
        self.assertIn('200', self.store.data['records']['100'])
        await self.service.handle_leave(self.bot, self.event(sub_type='leave'))
        self.assertNotIn('200', self.store.data['records'].get('100', {}))

    async def test_cq_placeholder_in_user_input_does_not_become_extra_at(self):
        self.store.data['records']['100'] = {'200': {'category': 'GitHub'}}
        self.bot.roles[200] = 'member'
        await self.join()
        s = self.service.pending('900', 100, 200)
        await self.service.handle_answer(self.bot, self.answer(str(s['answer'])))
        sent = self.bot.actions('send_group_msg')
        self.assertEqual(sum('验证成功' in str(x['message']) for x in sent), 1)
        # 验证成功与自定义欢迎应合在同一条，而不是连续欢迎两次。
        self.assertEqual(len(sent), 2)

    async def test_group_keyword_overrides_do_not_affect_other_group(self):
        self.repo.settings.approval = copy.copy(self.repo.settings.approval)
        self.repo.settings.approval.group_rules = {'100': {'accept_categories': ['频道:指定词'], 'reject_key': []}}
        await self.service.handle_request(self.bot, self.event(comment='github', flag='one', sub_type='add'))
        self.assertEqual(self.bot.actions('set_group_add_request'), [])
        await self.service.handle_request(self.bot, self.event(101, comment='github', flag='two', sub_type='add'))
        self.assertTrue(self.bot.actions('set_group_add_request')[0]['approve'])

    async def test_live_switch_off_during_web_check_cannot_welcome_or_penalize(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        self.backend.gate = asyncio.Event()
        before = len(self.bot.actions('send_group_msg'))
        task = asyncio.create_task(self.service.handle_answer(self.bot, self.answer('000042')))
        await asyncio.sleep(0)
        self.repo.settings.enabled = False
        self.backend.gate.set()
        await task
        self.assertEqual(len(self.bot.actions('send_group_msg')), before)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_wrong_answer_after_final_deadline_is_not_given_another_full_timeout(self):
        a = await self.join()
        self.clock = 1101
        await self.service.handle_answer(self.bot, self.answer(str((int(a['answer']) + 1) % 101)))
        current = self.service.pending('900', 100, 200)
        self.assertEqual(current['status'], 'grace')
        self.assertLessEqual(current['kick_at'], self.clock + 5)

    async def test_answer_after_grace_deadline_cannot_pass_before_next_tick(self):
        a = await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        self.clock = 1107
        await self.service.handle_answer(self.bot, self.answer(str(a['answer'])))
        self.assertIsNotNone(self.service.pending('900', 100, 200))
        self.assertEqual(len(self.bot.actions('send_group_msg')), 2)

    async def test_member_left_before_delayed_prompt_does_not_get_punished(self):
        self.bot.fail_member = True
        await self.join()
        self.clock = 9999
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('send_group_msg'), [])
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_kick_removes_member_source_immediately(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        self.clock = 1107
        await self.service.tick(self.bot)
        self.assertNotIn('200', self.store.data['records'].get('100', {}))

    async def test_delayed_create_then_master_off_does_not_send_prompt(self):
        self.repo.update_group(100, enable_combined_verify=True)
        original = self.backend.create
        entered = asyncio.Event()
        release = asyncio.Event()
        async def wait_create(gid, uid):
            entered.set()
            await release.wait()
            return await original(gid, uid)
        self.backend.create = wait_create
        task = asyncio.create_task(self.join())
        await entered.wait()
        self.repo.settings.enabled = False
        release.set()
        await task
        self.assertEqual(self.bot.actions('send_group_msg'), [])
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_bot_leaving_during_web_call_invalidates_challenge(self):
        self.repo.update_group(100, enable_combined_verify=True)
        original = self.backend.create
        entered = asyncio.Event()
        release = asyncio.Event()
        async def wait_create(gid, uid):
            entered.set()
            await release.wait()
            return await original(gid, uid)
        self.backend.create = wait_create
        task = asyncio.create_task(self.join())
        await entered.wait()
        await self.service.handle_leave(self.bot, self.event(uid=900, sub_type='kick_me'))
        release.set()
        await task
        self.assertEqual(self.bot.actions('send_group_msg'), [])
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_disabled_switch_during_nickname_lookup_does_not_approve(self):
        original = self.bot.call_api
        async def api(name, **kw):
            result = await original(name, **kw)
            if name == 'get_stranger_info':
                self.repo.settings.enabled = False
            return result
        self.bot.call_api = api
        await self.service.handle_request(self.bot, self.event(comment='github', flag='one', sub_type='add'))
        self.assertEqual(self.bot.actions('set_group_add_request'), [])

    async def test_refresh_after_backend_check_rejects_stale_generation(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        original = self.backend.check
        async def replace_state(*args):
            key = self.service._key('900', 100, 200)
            self.store.data['sessions'][key]['token'] = 'new-generation'
            return await original(*args)
        self.backend.check = replace_state
        await self.service.handle_answer(self.bot, self.answer('000042'))
        self.assertIsNotNone(self.service.pending('900', 100, 200))
        self.assertEqual(self.service.pending('900', 100, 200)['token'], 'new-generation')

    async def test_bot_departure_while_prompt_send_fails_does_not_resurrect_session(self):
        original = self.bot.call_api
        async def api(name, **kw):
            if name == 'send_group_msg':
                await self.service.handle_leave(self.bot, self.event(uid=900, sub_type='kick_me'))
                raise RuntimeError('bot left')
            return await original(name, **kw)
        self.bot.call_api = api
        await self.join()
        self.assertEqual(self.store.data['sessions'], {})

    async def test_new_session_token_survives_stale_kick_metadata_return(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        original = self.bot.call_api
        async def api(name, **kw):
            result = await original(name, **kw)
            if name == 'get_group_member_info' and kw['user_id'] == 200:
                key = self.service._key('900', 100, 200)
                self.store.data['sessions'][key]['token'] = 'fresh-rejoin'
            return result
        self.bot.call_api = api
        self.clock = 1110
        await self.service.tick(self.bot)
        self.assertEqual(self.service.pending('900', 100, 200)['token'], 'fresh-rejoin')
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_manual_notice_without_verification_sends_only_once(self):
        self.repo.update_group(100, enabled=False)
        await self.join()
        await self.join()
        self.assertEqual(len(self.bot.actions('send_group_msg')), 1)

    async def test_original_template_comment_is_available_in_join_welcome(self):
        self.repo.settings.approval = copy.deepcopy(self.repo.settings.approval)
        self.repo.settings.approval.msg = dict(self.repo.settings.approval.msg, welcome_msg=['default:%comment%'])
        await self.service.handle_request(self.bot, self.event(comment='GitHub 的甲', flag='one', sub_type='add'))
        await self.join()
        s = self.service.pending('900', 100, 200)
        await self.service.handle_answer(self.bot, self.answer(str(s['answer'])))
        self.assertIn('GitHub 的甲', str(self.bot.actions('send_group_msg')[-1]['message']))

    async def test_source_chart_selects_existing_random_background_directory(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='guard-chart-service-') as td:
            assets = Path(td)
            (assets / 'background').mkdir()
            (assets / 'background/one.png').write_bytes(b'not-a-real-image')
            approval = copy.copy(self.repo.settings.approval)
            approval.chart_enabled = lambda gid: True
            approval.assets_dir = str(assets)
            self.repo.settings.approval = approval
            self.store.data['records']['100'] = {'200': {'category': 'GitHub'}}
            renderer = types.ModuleType(PKG + '.charts')
            called = []
            def render(gid, records, **kwargs):
                called.append(kwargs)
                return b'fake-only'
            renderer.render_chart = render
            with patch.dict(sys.modules, {renderer.__name__: renderer}):
                self.assertEqual(await self.service._chart(100), b'fake-only')
            self.assertEqual(Path(called[0]['background_path']), assets / 'background/one.png')

    async def test_manual_cancel_during_web_wait_cannot_resume_on_quick_reenable(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        original = self.backend.check
        async def cancel(*args):
            key = self.service._key('900', 100, 200)
            self.store.data['sessions'][key]['status'] = 'manual'
            return await original(*args)
        self.backend.check = cancel
        before = len(self.bot.actions('send_group_msg'))
        await self.service.handle_answer(self.bot, self.answer('000042'))
        self.assertEqual(self.store.data['sessions']['900:100:200']['status'], 'manual')
        self.assertEqual(len(self.bot.actions('send_group_msg')), before)

    async def test_bot_left_during_failed_web_reminder_cannot_recreate_session(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        async def create(*args):
            await self.service.handle_leave(self.bot, self.event(uid=900, sub_type='kick_me'))
            raise RuntimeError('service unavailable')
        self.backend.create = create
        self.clock = 1081
        await self.service.tick(self.bot)
        self.assertEqual(self.store.data['sessions'], {})

    async def test_no_penalty_if_join_metadata_has_zero_join_time(self):
        self.bot.join_time = 0
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        self.clock = 1107
        await self.service.tick(self.bot)
        self.assertEqual(self.bot.actions('set_group_kick'), [])

    async def test_admin_reverify_does_not_skip_on_high_qq_level(self):
        self.repo.update_group(100, enable_level_verify=True)
        self.bot.level = 50
        await self.join()
        self.assertIsNone(self.service.pending('900', 100, 200))
        await self.service.reverify(self.bot, 100, 200)
        self.assertIsNotNone(self.service.pending('900', 100, 200))

    async def test_user_supplied_at_placeholder_stays_plain_in_approval_notice(self):
        await self.service.handle_request(self.bot, self.event(comment='github {at_user} %at_user% [CQ:at,qq=all]', flag='safe', sub_type='add'))
        text = self.bot.actions('send_group_msg')[0]['message']['text']
        self.assertNotIn('{at_user}', text)
        self.assertNotIn('%at_user%', text)

    async def test_late_approval_updates_manual_classification_without_erasing_join_time(self):
        await self.join()
        previous = self.store.data['records']['100']['200']['accept_time']
        await self.service.handle_request(self.bot, self.event(comment='github', flag='late', sub_type='add'))
        record = self.store.data['records']['100']['200']
        self.assertEqual(record['category'], 'GitHub')
        self.assertEqual(record['accept_time'], previous)

    async def test_successful_verification_blocks_duplicate_join_notice_with_new_time(self):
        s = await self.join()
        await self.service.handle_answer(self.bot, self.answer(str(s['answer'])))
        before = len(self.bot.actions('send_group_msg'))
        await self.service.handle_join(self.bot, self.event(time=1002))
        self.assertIsNone(self.service.pending('900', 100, 200))
        self.assertEqual(len(self.bot.actions('send_group_msg')), before)

    async def test_restore_queued_behind_correct_answer_does_not_reopen_verified(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        self.backend.gate = asyncio.Event()
        answer_task = asyncio.create_task(self.service.handle_answer(self.bot, self.answer('000042')))
        await asyncio.sleep(0)
        restore_task = asyncio.create_task(self.service.restore(self.bot))
        await asyncio.sleep(0)
        self.backend.gate.set()
        await asyncio.gather(answer_task, restore_task)
        self.assertIsNone(self.service.pending('900', 100, 200))
        self.assertEqual(sum(call[0] == 'create' for call in self.backend.calls), 1)

    async def test_restore_member_lookup_cannot_overwrite_cancelled_session(self):
        await self.join()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 200:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        task = asyncio.create_task(self.service.restore(self.bot))
        await entered.wait()
        self.store.data['sessions']['900:100:200']['status'] = 'manual'
        release.set()
        await task
        self.assertEqual(self.store.data['sessions']['900:100:200']['status'], 'manual')

    async def test_bypass_during_final_permission_lookup_prevents_kick(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 900:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        self.clock = 1107
        tick = asyncio.create_task(self.service.tick(self.bot))
        await entered.wait()
        bypass = asyncio.create_task(self.service.bypass(self.bot, 100, 200))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(tick, bypass)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.assertTrue(self.store.data['bypass']['900:100:200'])

    async def test_leave_rejoin_queued_during_permission_lookup_prevents_old_kick(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 900:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        self.clock = 1107
        tick = asyncio.create_task(self.service.tick(self.bot))
        await entered.wait()
        leave = asyncio.create_task(self.service.handle_leave(self.bot, self.event(sub_type='leave')))
        await asyncio.sleep(0)
        self.bot.join_time = 20
        rejoin = asyncio.create_task(self.service.handle_join(self.bot, self.event(time=1108)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(tick, leave, rejoin)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.assertEqual(self.service.pending('900', 100, 200)['join_time'], 20)

    async def test_retry_member_lookup_cannot_overwrite_cancelled_session(self):
        for method in ('backend-error', 'wrong-answer'):
            with self.subTest(method=method):
                self.store = MemoryStore()
                self.backend = FakeBackend()
                self.service = S.GuardService(self.repo, self.store, backend_factory=lambda *a, **k: self.backend, now=lambda: self.clock)
                self.repo.update_group(100, enable_combined_verify=method == 'backend-error')
                session = await self.join()
                self.backend.check_error = True
                entered, release = asyncio.Event(), asyncio.Event()
                original = self.bot.call_api
                async def call(api, **kw):
                    if api == 'get_group_member_info' and kw['user_id'] == 200:
                        entered.set()
                        await release.wait()
                    return await original(api, **kw)
                self.bot.call_api = call
                text = '000042' if method == 'backend-error' else str((session['answer'] + 1) % 101)
                task = asyncio.create_task(self.service.handle_answer(self.bot, self.answer(text)))
                await entered.wait()
                self.store.data['sessions']['900:100:200']['status'] = 'manual'
                release.set()
                await task
                self.assertEqual(self.store.data['sessions']['900:100:200']['status'], 'manual')
                self.bot.call_api = original
                await self.service.close()

    async def test_reminder_member_lookup_cannot_overwrite_cancelled_session(self):
        self.repo.update_group(100, enable_combined_verify=True)
        await self.join()
        self.backend.create_error = True
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 200:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        self.clock = 1081
        task = asyncio.create_task(self.service.tick(self.bot))
        await entered.wait()
        self.store.data['sessions']['900:100:200']['status'] = 'manual'
        release.set()
        await task
        self.assertEqual(self.store.data['sessions']['900:100:200']['status'], 'manual')

    async def test_grace_wrong_answer_cannot_extend_original_kick_time(self):
        session = await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        old_kick = self.service.pending('900', 100, 200)['kick_at']
        self.clock = 1102
        await self.service.handle_answer(self.bot, self.answer(str((session['answer'] + 1) % 101)))
        current = self.service.pending('900', 100, 200)
        self.assertEqual(current['status'], 'grace')
        self.assertEqual(current['kick_at'], old_kick)

    async def test_reverify_queued_during_permission_lookup_invalidates_old_penalty(self):
        await self.join()
        self.clock = 1101
        await self.service.tick(self.bot)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 900:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        self.clock = 1107
        tick = asyncio.create_task(self.service.tick(self.bot))
        await entered.wait()
        reverify = asyncio.create_task(self.service.reverify(self.bot, 100, 200))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(tick, reverify)
        self.assertEqual(self.bot.actions('set_group_kick'), [])
        self.assertGreaterEqual(self.service.pending('900', 100, 200)['deadline'], self.clock + 100)

    async def test_bypass_during_level_lookup_cannot_start_another_challenge(self):
        self.repo.update_group(100, enable_level_verify=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_stranger_info':
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        task = asyncio.create_task(self.join())
        await entered.wait()
        await self.service.bypass(self.bot, 100, 200)
        release.set()
        await task
        self.assertIsNone(self.service.pending('900', 100, 200))

    async def test_leave_during_first_member_lookup_cannot_create_challenge(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_group_member_info' and kw['user_id'] == 200:
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        task = asyncio.create_task(self.join())
        await entered.wait()
        leave = asyncio.create_task(self.service.handle_leave(self.bot, self.event(sub_type='leave')))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, leave)
        self.assertIsNone(self.service.pending('900', 100, 200))
        self.assertEqual([call['message']['text'] for call in self.bot.actions('send_group_msg')], ['200 已离开'])

    async def test_cancel_during_first_level_lookup_survives_quick_reenable(self):
        self.repo.update_group(100, enable_level_verify=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.call_api
        async def call(api, **kw):
            if api == 'get_stranger_info':
                entered.set()
                await release.wait()
            return await original(api, **kw)
        self.bot.call_api = call
        task = asyncio.create_task(self.join())
        await entered.wait()
        await self.service.disable_group(self.bot, 100)
        self.repo.update_group(100, enabled=True)
        release.set()
        await task
        self.assertIsNone(self.service.pending('900', 100, 200))
        self.assertEqual(self.bot.actions('send_group_msg'), [])

    async def test_join_queued_before_leave_behind_other_network_work_is_cancelled(self):
        lock = self.service._lock('900:100:200')
        await lock.acquire()
        try:
            join = asyncio.create_task(self.join())
            await asyncio.sleep(0)
            leave = asyncio.create_task(self.service.handle_leave(self.bot, self.event(sub_type='leave')))
            await asyncio.sleep(0)
        finally:
            lock.release()
        await asyncio.gather(join, leave)
        self.assertNotIn('900:100:200', self.store.data['sessions'])
        self.assertEqual([call['message']['text'] for call in self.bot.actions('send_group_msg')], ['200 已离开'])

    async def test_rule_approval_records_method_without_inventing_source(self):
        self.repo.settings.approval.group_rules = {'100': {
            'approval_mode': 'rules',
            'accept_rules': [{'name': '成员知识题', 'match': 'exact', 'values': ['花帆', '吟子']}],
        }}
        await self.service.handle_request(self.bot, self.event(comment='问题：喜欢的成员\n答案：吟子', flag='rules-1', sub_type='add'))
        record = self.store.data['records']['100']['200']
        self.assertIsNone(record['source_category'])
        self.assertEqual(record['approval_rule'], '成员知识题')
        self.assertEqual(record['approval_method'], 'rules')
        self.assertIn('命中允许规则', record['accept_reason'])
        text = self.bot.actions('send_group_msg')[0]['message']['text']
        self.assertIn('审批规则：成员知识题', text)
        self.assertNotIn('来源：成员知识题', text)

    async def test_rule_source_mapping_is_explicit_and_optional(self):
        self.repo.settings.approval.group_rules = {'100': {
            'approval_mode': 'rules', 'source_statistics': True,
            'accept_rules': [{'name': '频道入口', 'match': 'contains', 'values': ['github'], 'source': 'GitHub'}],
        }}
        await self.service.handle_request(self.bot, self.event(comment='GitHub', flag='mapped', sub_type='add'))
        record = self.store.data['records']['100']['200']
        self.assertEqual(record['source_category'], 'GitHub')
        self.assertEqual(record['approval_rule'], '频道入口')
        self.assertIn('来源：GitHub', self.bot.actions('send_group_msg')[0]['message']['text'])

    async def test_late_rule_approval_keeps_auto_welcome_without_source(self):
        self.repo.settings.approval.group_rules = {'100': {
            'approval_mode': 'rules',
            'accept_rules': [{'name': '知识题', 'match': 'exact', 'values': ['吟子']}],
        }}
        session = await self.join()
        await self.service.handle_request(self.bot, self.event(comment='吟子', flag='late-rule', sub_type='add'))
        await self.service.handle_answer(self.bot, self.answer(str(session['answer'])))
        welcome = self.bot.actions('send_group_msg')[-1]['message']['text']
        self.assertIn('欢迎 成员', welcome)
        self.assertNotIn('欢迎人工审核成员', welcome)
        self.assertEqual(self.store.data['records']['100']['200']['approval_method'], 'rules')

    async def test_request_question_does_not_authorize_wrong_answer(self):
        self.repo.settings.approval.group_rules = {'100': {
            'approval_mode': 'rules',
            'accept_rules': [{'name': '知识题', 'match': 'exact', 'values': ['花帆']}],
        }}
        await self.service.handle_request(self.bot, self.event(comment='问题：请输入花帆\n答案：不知道', flag='wrong-question', sub_type='add'))
        self.assertEqual(self.bot.actions('set_group_add_request'), [])

    async def test_fixed_answer_is_not_disclosed_in_approval_notice(self):
        self.repo.settings.approval.group_rules = {'100': {
            'approval_mode': 'rules',
            'accept_rules': [{'name': '固定口令', 'match': 'exact', 'values': ['neutral-passphrase']}],
        }}
        from unittest.mock import patch
        decision = NS(action='approve', method='rules', rule='固定口令', category='', keyword='neutral-passphrase')
        with patch.object(S, 'decide_request', return_value=decision):
            await self.service.handle_request(self.bot, self.event(comment='neutral-passphrase', flag='secret-answer', sub_type='add'))
        self.assertEqual(len(self.bot.actions('set_group_add_request')), 1)
        text = self.bot.actions('send_group_msg')[0]['message']['text']
        self.assertNotIn('neutral-passphrase', text)
        self.assertIn('审批规则：固定口令', text)

    async def test_approval_rule_change_during_lookup_is_rechecked_before_action(self):
        from unittest.mock import patch
        allowed = NS(action='approve', category='GitHub', keyword='github', method='legacy', rule='')
        manual = NS(action='manual', category='', keyword='', method='manual', rule='')
        with patch.object(S, 'decide_request', side_effect=[allowed, manual]):
            await self.service.handle_request(self.bot, self.event(comment='github', flag='changed-rules', sub_type='add'))
        self.assertEqual(self.bot.actions('set_group_add_request'), [])

    async def test_new_manual_approval_is_not_a_source_category(self):
        await self.join()
        record = self.store.data['records']['100']['200']
        self.assertEqual(record['approval_method'], 'manual')
        self.assertIsNone(record['source_category'])
        self.assertEqual(record['category'], '人工审核')

    async def test_late_legacy_approval_updates_new_metadata_after_mode_switch(self):
        self.repo.settings.approval.group_rules = {'100': {'approval_mode': 'rules'}}
        await self.join()
        self.repo.settings.approval.group_rules = {'100': {'approval_mode': 'legacy'}}
        await self.service.handle_request(self.bot, self.event(comment='github', flag='late-legacy', sub_type='add'))
        record = self.store.data['records']['100']['200']
        self.assertEqual(record['approval_method'], 'legacy')
        self.assertEqual(record['source_category'], 'GitHub')

    async def test_disk_failure_happens_before_prompt_or_penalty(self):
        self.store.fail = True
        with self.assertRaises(OSError):
            await self.join()
        self.assertEqual(self.bot.actions('send_group_msg'), [])
        self.assertEqual(self.bot.actions('set_group_kick'), [])


if __name__ == '__main__':
    unittest.main()
