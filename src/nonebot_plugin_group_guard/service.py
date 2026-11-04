"""群管业务服务，所有外部操作经传入的 OneBot 客户端。"""
import asyncio
import copy
import hashlib
import logging
import time
import uuid
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .policy import (
    decide_request, generate_problem, parse_answer, parse_target, qq_level,
    render_template, template_for,
)

logger = logging.getLogger(__name__)


class GuardService:
    def __init__(self, repo, store, *, backend_factory=None, now=time.time):
        self.repo, self.store, self.now = repo, store, now
        self.backend_factory = backend_factory
        self._backends = {}
        self._locks = weakref.WeakValueDictionary()

    def _key(self, sid, gid, uid):
        return f'{sid}:{gid}:{uid}'

    def _lock(self, key):
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def active(self, bot):
        settings = self.repo.settings
        return settings.enabled and (not settings.self_ids or str(bot.self_id) in settings.self_ids)

    def enabled_group(self, bot, gid):
        return self.active(bot) and (
            self.repo.settings.approval.allows(gid) or self._cfg(gid)['enabled']
        )

    def _cfg(self, gid):
        return self.repo.settings.verification.for_group(gid)

    def pending(self, sid, gid, uid):
        item = self.store.data['sessions'].get(self._key(sid, gid, uid))
        return copy.deepcopy(item) if item and item['status'] in ('pending', 'grace') else None

    def _current(self, bot, key, session):
        current = self.store.data['sessions'].get(key)
        return (self.active(bot) and self._cfg(session['group_id'])['enabled']
                and current is not None and current['token'] == session['token']
                and current['status'] not in ('manual', 'verified', 'cancelled'))

    def _matches(self, key, expected):
        current = self.store.data['sessions'].get(key)
        if expected is None:
            return current is None
        return (current is not None and current['token'] == expected['token']
                and current['status'] == expected['status'])

    def _put(self, key, session):
        self.store.transaction(lambda data: data['sessions'].__setitem__(key, copy.deepcopy(session)))

    def _drop(self, key):
        self.store.transaction(lambda data: data['sessions'].pop(key, None))

    def _invalidate(self, key, reason):
        """在第一次 await 前使旧处罚失效，不能排在持网络锁的任务之后。"""
        previous = copy.deepcopy(self.store.data['sessions'].get(key))
        if previous is None:
            # 入群任务可能尚在等锁；空状态也要留取消轮次，防止它稍后凭空建验。
            sid, gid, uid = key.split(':')
            previous = {'self_id': sid, 'group_id': int(gid), 'user_id': int(uid),
                        'join_time': None, 'wrong_count': 0, 'started': self.now()}
        marker = dict(previous, token=uuid.uuid4().hex, status='cancelled', reason=reason)
        self._put(key, marker)
        return marker

    def _manual(self, key, session, reason):
        if self.store.data['sessions'].get(key, {}).get('token') != session['token']:
            return
        item = dict(session, status='manual', reason=reason)
        self._put(key, item)
        logger.warning('群管流程转为人工处理：%s', reason)

    async def _member(self, bot, gid, uid):
        try:
            info = await bot.call_api('get_group_member_info', group_id=int(gid), user_id=int(uid), no_cache=True)
            return info if isinstance(info, dict) else None
        except Exception:
            return None

    async def _send(self, bot, gid, text, *, uid=None, image=None, private=False):
        if not self.active(bot):
            return False
        from .messages import to_message
        message = to_message(text, at_user=uid, image=image)
        api, field = ('send_private_msg', 'user_id') if private else ('send_group_msg', 'group_id')
        try:
            await bot.call_api(api, **{field: int(gid), 'message': message})
            return True
        except Exception as error:
            logger.warning('群管消息发送失败：%s', type(error).__name__)
            return False

    def _values(self, gid, uid, **extra):
        return {'group_id': gid, 'user_id': uid, 'user_name': str(uid), 'member_name': str(uid), **extra}

    def _text(self, text, values):
        # at 占位符留给消息构造层，用户输入永远不是 CQ 代码。
        safe = {k: str(v).replace('{at_user}', '｛at_user｝').replace('%at_user%', '％at_user％')
                for k, v in values.items() if k != 'at_user'}
        return render_template(text, safe)

    async def _chart(self, gid):
        approval = self.repo.settings.approval
        if not approval.chart_enabled(gid):
            return None
        records = copy.deepcopy(self.store.data['records'].get(str(gid), {}))
        if not records:
            return None
        from .charts import render_chart
        assets = Path(approval.assets_dir) if approval.assets_dir else None
        font = str(assets / approval.font) if assets and approval.font else ''
        background = str(assets / approval.bg_img) if assets and approval.bg_img else ''
        if assets:
            directory = assets / 'background'
            if directory.is_dir():
                # 保留原统计图的随机卡面背景，目录可为本机素材库软链接。
                choices = [path for path in directory.iterdir()
                           if path.is_file() and path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')]
                if choices:
                    import random
                    background = str(random.SystemRandom().choice(choices))
        try:
            return await asyncio.to_thread(render_chart, gid, records, font_path=font, background_path=background)
        except Exception as error:
            logger.warning('群管统计图绘制失败：%s', type(error).__name__)
            return None

    async def _notify(self, bot, kind, gid, text, *, uid=None, with_chart=False, skip_origin=False):
        approval = self.repo.settings.approval
        if not approval.allows(gid):
            return
        targets = set()
        for raw in approval.notice.get(kind, []):
            try:
                target = parse_target(raw, gid)
            except ValueError:
                logger.warning('已忽略无效的群管通知目标')
                continue
            if not (skip_origin and target == ('group', int(gid))):
                targets.add(target)
        image = await self._chart(gid) if with_chart and targets else None
        for kind_, target in sorted(targets):
            await self._send(bot, target, text, uid=uid if kind_ == 'group' and target == int(gid) else None,
                             image=image, private=kind_ == 'private')
            if approval.delay:
                await asyncio.sleep(approval.delay)

    def _record_join(self, gid, uid, category='人工审核', reason='人工审核', comment='', *,
                     method='manual', rule='', source=None):
        approval = self.repo.settings.approval
        if not approval.allows(gid):
            return
        def update(data):
            records = data['records'].setdefault(str(gid), {})
            values = {'category': category, 'accept_reason': reason, 'comment': comment,
                      'source_category': source, 'approval_method': method, 'approval_rule': rule}
            record = records.setdefault(str(uid), dict(values,
                accept_time=datetime.fromtimestamp(self.now()).strftime('%Y-%m-%d %H:%M:%S')))
            manual = record.get('approval_method', 'manual' if record.get('category') == '人工审核' else 'legacy') == 'manual'
            if method != 'manual' and manual:
                record.update(values)
        self.store.transaction(update)

    async def handle_request(self, bot, event):
        gid, uid = event.group_id, event.user_id
        if not self.active(bot) or event.sub_type != 'add' or str(uid) == str(bot.self_id):
            return
        approval = self.repo.settings.approval
        decision = decide_request(approval, gid, event.comment)
        if decision.action not in ('approve', 'reject') or not getattr(event, 'flag', ''):
            return
        key = self._key(bot.self_id, gid, uid)
        request_key = hashlib.sha256(f'{bot.self_id}:{gid}:{event.flag}'.encode()).hexdigest()
        async with self._lock(key):
            if request_key in self.store.data['requests'] or not self.active(bot):
                return
            def claim(data):
                data['requests'][request_key] = {'status': 'processing', 'time': self.now()}
                while len(data['requests']) > 10000:
                    data['requests'].pop(next(iter(data['requests'])))
            self.store.transaction(claim)
            name = str(uid)
            try:
                info = await bot.call_api('get_stranger_info', user_id=int(uid))
                name = info.get('nickname') or info.get('nick') or name
            except Exception:
                pass
            if not self.active(bot) or not self.repo.settings.approval.allows(gid):
                return
            approval = self.repo.settings.approval
            decision = decide_request(approval, gid, event.comment)
            if decision.action not in ('approve', 'reject'):
                self.store.transaction(lambda data: data['requests'][request_key].update(status='manual'))
                return
            values = self._values(gid, uid, user_name=name, member_name=name,
                                  category=decision.category, key=decision.keyword, comment=event.comment)
            reason = self._text(template_for(approval.msg.get('reject_reason', []), gid, '触发拒绝关键词'), values)
            try:
                await bot.call_api('set_group_add_request', flag=event.flag, sub_type='add',
                                   approve=decision.action == 'approve', reason=reason if decision.action == 'reject' else '')
            except Exception as error:
                self.store.transaction(lambda data: data['requests'][request_key].update(status='failed'))
                logger.warning('群管审批操作失败：%s', type(error).__name__)
                return
            self.store.transaction(lambda data: data['requests'][request_key].update(status='done'))
            if decision.action == 'approve':
                reason = ('匹配关键词: ' + decision.keyword if decision.method == 'legacy'
                          else '命中允许规则: ' + decision.rule)
                self._record_join(gid, uid, decision.category, reason, event.comment,
                                  method=decision.method, rule=decision.rule, source=decision.category or None)
                values.update(rule=decision.rule, reason=reason, category=decision.category or '未记录来源')
                text = '群 {group_id} 已自动通过 {user_id} 的入群申请。'
                if decision.rule:
                    text += '\n审批规则：{rule}'
                if decision.category:
                    text += '\n来源：{category}'
                if decision.method == 'legacy':
                    text += '\n验证消息：{comment}'
                await self._notify(bot, 'accept_notice', gid, self._text(text, values), uid=uid, with_chart=True)
            else:
                text = f'群 {gid} 已自动拒绝 {uid} 的入群申请。\n原因：{reason}'
                await self._notify(bot, 'reject_notice', gid, text)

    async def handle_join(self, bot, event, *, force=False):
        gid, uid = event.group_id, event.user_id
        if not self.enabled_group(bot, gid) or str(uid) == str(bot.self_id):
            return
        key = self._key(bot.self_id, gid, uid)
        lock = self._lock(key)
        marker = self._invalidate(key, '重新验证' if force else '入群事件待核对') if force or lock.locked() else None
        async with lock:
            if not self.enabled_group(bot, gid):
                return
            if marker is not None and not self._matches(key, marker):
                return
            session = copy.deepcopy(self.store.data['sessions'].get(key))
            initial = session is None
            if initial:
                # 首次成员查询也可能被取消，先建立可失效的轮次，不能用空状态充当身份。
                session = {'self_id': str(bot.self_id), 'group_id': int(gid), 'user_id': int(uid),
                           'token': uuid.uuid4().hex, 'status': 'checking', 'wrong_count': 0,
                           'join_time': None, 'started': self.now()}
                self._put(key, session)
            member = await self._member(bot, gid, uid)
            if not self.active(bot) or not self._matches(key, session):
                return
            if member is None:
                self._manual(key, session, '无法确认入群成员，暂停自动验证')
                return
            if not initial and not force:
                if not member.get('join_time') or member.get('join_time') == session.get('join_time'):
                    if marker is not None:
                        self._manual(key, session, '重复入群通知中断验证流程，转人工核对')
                    return
            if not force and self.store.data['notices'].get('join:' + key) == event.time:
                return
            self._record_join(gid, uid)
            def mark(data):
                data['notices']['join:' + key] = event.time
                while len(data['notices']) > 10000:
                    data['notices'].pop(next(iter(data['notices'])))
            self.store.transaction(mark)
            cfg = self._cfg(gid)
            bypassed = bool(self.store.data['bypass'].get(key)) and not force
            exempt = member.get('role') in ('admin', 'owner') or bypassed
            if cfg['enabled'] and not exempt and cfg['enable_level_verify'] and not force:
                try:
                    level = qq_level(await bot.call_api('get_stranger_info', user_id=int(uid)))
                    exempt = level is not None and level >= cfg['min_qq_level']
                except Exception:
                    pass
            if not self.active(bot) or not self._matches(key, session):
                return
            if force:
                self.store.transaction(lambda data: data['bypass'].pop(key, None))
            if not cfg['enabled'] or exempt:
                if session:
                    self._drop(key)
                await self._welcome(bot, gid, uid, member)
                return
            await self._start(bot, gid, uid, member, cfg, wrong_count=0, expected_session=session)

    def _backend(self):
        config = self.repo.settings.verification
        secret = config.api_key.get_secret_value()
        key = (config.api_base_url, secret)
        if key not in self._backends:
            if self.backend_factory is None:
                from .web import VerificationBackend
                factory = VerificationBackend
            else:
                factory = self.backend_factory
            self._backends[key] = factory(config.api_base_url, secret)
        return self._backends[key]

    async def _start(self, bot, gid, uid, member, cfg, *, wrong_count=0, force_math=False, expected_session=None):
        key = self._key(bot.self_id, gid, uid)
        if (not self.active(bot) or not self._cfg(gid)['enabled']
                or not self._matches(key, expected_session) or self.store.data['bypass'].get(key)):
            return
        question, answer = generate_problem()
        now = self.now()
        session = {'self_id': str(bot.self_id), 'group_id': int(gid), 'user_id': int(uid),
            'token': uuid.uuid4().hex, 'status': 'preparing', 'method': 'math',
            'question': question, 'answer': answer, 'wrong_count': wrong_count,
            'deadline': now + cfg['verification_timeout'], 'warned': False,
            'join_time': member.get('join_time'), 'member_name': member.get('card') or member.get('nickname') or str(uid),
            'last_hint': 0, 'started': now}
        self._put(key, session)
        if cfg['verify_delay'] > 0 and wrong_count == 0:
            await asyncio.sleep(cfg['verify_delay'])
        if not self.enabled_group(bot, gid) or not self._cfg(gid)['enabled']:
            if self._matches(key, session):
                self._drop(key)
            return
        if cfg['enable_combined_verify'] and not force_math:
            try:
                url = await self._backend().create(gid, uid)
                session.update(method='combined', url=url)
            except Exception as error:
                logger.warning('网页验证暂不可用，改用本地算术题：%s', type(error).__name__)
        if not self._current(bot, key, session):
            if self._matches(key, session):
                self._drop(key)
            return
        values = self._values(gid, uid, member_name=session['member_name'],
            user_name=session['member_name'], question=question, timeout=cfg['verification_timeout'] / 60,
            timeout_seconds=cfg['verification_timeout'], remaining=cfg['max_wrong_answers'] - wrong_count,
            count=wrong_count)
        if session['method'] == 'combined':
            values['url'] = session['url']
            field = 'web_wrong_answer_prompt' if wrong_count else 'web_member_prompt'
            default = ('{at_user} 验证码错误，请使用新链接重试：\n{url}' if wrong_count else
                       '{at_user} 请在 {timeout_seconds} 秒内打开链接，完成后发送六位验证码：\n{url}')
            text = cfg.get(field, getattr(self.repo.settings.verification, field, default))
        else:
            config = self.repo.settings.verification
            field = 'wrong_answer_prompt' if wrong_count else 'new_member_prompt'
            text = cfg.get(field, getattr(config, field))
        session['deadline'] = self.now() + cfg['verification_timeout']
        session['status'] = 'pending'
        self._put(key, session)
        if not await self._send(bot, gid, self._text(text, values), uid=uid):
            self._manual(key, session, '验证提示未能送达，暂停自动处罚')

    async def _welcome(self, bot, gid, uid, member, *, verified=False):
        approval = self.repo.settings.approval
        record = self.store.data['records'].get(str(gid), {}).get(str(uid), {})
        category = record.get('source_category', record.get('category')) or '未记录来源'
        name = member.get('card') or member.get('nickname') or str(uid)
        values = self._values(gid, uid, user_name=name, member_name=name, category=category, comment=record.get('comment', ''))
        if rule := record.get('approval_rule'):
            values['rule'] = rule
        values['approval_method'] = record.get('approval_method', '')
        method = record.get('approval_method', 'manual' if record.get('category', '人工审核') == '人工审核' else 'legacy')
        kind = 'increase_msg' if method == 'manual' else 'welcome_msg'
        text = template_for(approval.msg.get(kind, []), gid, '欢迎加入本群。')
        if verified:
            passed = self._text(self._cfg(gid).get('welcome_message', self.repo.settings.verification.welcome_message), values)
            text = passed + ('\n' + self._text(text, values) if text else '')
        else:
            text = '{at_user} ' + self._text(text, values) if text else ''
        if not text:
            return
        if approval.allows(gid):
            await self._notify(bot, 'increase_notice', gid, text, uid=uid, with_chart=True)
        else:
            await self._send(bot, gid, text, uid=uid)

    async def handle_leave(self, bot, event):
        gid, uid = event.group_id, event.user_id
        if not self.active(bot):
            return
        if str(uid) == str(bot.self_id):
            prefix = f'{bot.self_id}:{gid}:'
            def cleanup(data):
                for key in list(data['sessions']):
                    if key.startswith(prefix):
                        data['sessions'].pop(key)
                data['records'].pop(str(gid), None)
            self.store.transaction(cleanup)
            return
        key = self._key(bot.self_id, gid, uid)
        if not self.enabled_group(bot, gid) and key not in self.store.data['sessions']:
            return
        marker = self._invalidate(key, '成员已离开')
        async with self._lock(key):
            def cleanup(data):
                current = data['sessions'].get(key)
                if marker is not None and current and current['token'] == marker['token']:
                    data['sessions'].pop(key, None)
                data['notices'].pop('join:' + key, None)
                data['records'].get(str(gid), {}).pop(str(uid), None)
            self.store.transaction(cleanup)
            approval = self.repo.settings.approval
            if approval.allows(gid) and approval.notice.get('decrease_notice'):
                text = template_for(approval.msg.get('decrease_msg', []), gid, '%user_id% 已离开本群。')
                if text:
                    await self._notify(bot, 'decrease_notice', gid, self._text(text, self._values(gid, uid)))

    def _plain_answer(self, event):
        pieces = []
        for segment in event.message:
            kind = segment.get('type') if isinstance(segment, dict) else segment.type
            data = segment.get('data', {}) if isinstance(segment, dict) else segment.data
            if kind == 'text':
                pieces.append(data.get('text', ''))
            elif kind not in ('at', 'reply'):
                return None
        return ''.join(pieces).strip()

    async def handle_answer(self, bot, event):
        gid, uid = event.group_id, event.user_id
        if not self.active(bot) or not self._cfg(gid)['enabled'] or str(uid) == str(bot.self_id):
            return False
        key = self._key(bot.self_id, gid, uid)
        async with self._lock(key):
            session = self.pending(bot.self_id, gid, uid)
            if session is None:
                return False
            text = self._plain_answer(event)
            answer = parse_answer(text, session['method']) if text is not None else None
            cfg = self._cfg(gid)
            if session['status'] == 'pending' and self.now() >= session['deadline']:
                await self._grace(bot, key, session, cfg)
                return True
            if session['status'] == 'grace' and self.now() >= session['kick_at']:
                return True
            if answer is None:
                if cfg.get('recall_unverified_messages'):
                    try:
                        await bot.call_api('delete_msg', message_id=event.message_id)
                    except Exception:
                        pass
                if self.now() - session.get('last_hint', 0) >= 30:
                    session['last_hint'] = self.now()
                    self._put(key, session)
                    await self._send(bot, gid, self._text(self.repo.settings.verification.error_verification,
                        self._values(gid, uid, timeout=cfg['verification_timeout'] / 60)), uid=uid)
                return False
            if session['status'] == 'grace' and session.get('failure_reason') == 'wrong':
                return True
            if session['method'] == 'combined':
                try:
                    valid = await self._backend().check(gid, uid, answer)
                except Exception as error:
                    if not self._current(bot, key, session):
                        return True
                    logger.warning('验证码后端异常，不计答错：%s', type(error).__name__)
                    member = await self._member(bot, gid, uid)
                    if member:
                        await self._start(bot, gid, uid, member, cfg, wrong_count=session['wrong_count'], force_math=True, expected_session=session)
                    else:
                        self._manual(key, session, '无法确认群成员状态')
                    return True
            else:
                valid = int(answer) == session['answer']
            if not self._current(bot, key, session):
                return True
            if valid:
                self._put(key, dict(session, status='verified'))
                member = {'nickname': session['member_name']}
                await self._welcome(bot, gid, uid, member, verified=True)
            else:
                wrong_count = session['wrong_count'] + 1
                if session['status'] == 'grace':
                    self._put(key, dict(session, wrong_count=wrong_count))
                    return True
                if wrong_count >= cfg['max_wrong_answers']:
                    session['wrong_count'] = wrong_count
                    await self._grace(bot, key, session, cfg, reason='wrong')
                else:
                    member = await self._member(bot, gid, uid)
                    if member:
                        await self._start(bot, gid, uid, member, cfg, wrong_count=wrong_count, expected_session=session)
                    else:
                        self._manual(key, session, '无法确认群成员状态')
            return True

    async def _grace(self, bot, key, session, cfg, *, reason='timeout'):
        if not self._current(bot, key, session):
            return
        config = self.repo.settings.verification
        delay = cfg.get('kick_delay', config.kick_delay)
        item = dict(session, status='grace', failure_reason=reason, kick_at=self.now() + delay)
        self._put(key, item)
        field = 'too_many_wrong_message' if reason == 'wrong' else 'failure_message'
        text = cfg.get(field, getattr(config, field))
        values = self._values(item['group_id'], item['user_id'], member_name=item['member_name'],
                              countdown=delay, count=item['wrong_count'])
        if not await self._send(bot, item['group_id'], self._text(text, values), uid=item['user_id']):
            self._manual(key, item, '超时通知未能送达，暂停自动处罚')

    async def tick(self, bot):
        if not self.active(bot):
            return
        keys = [key for key in self.store.data['sessions'] if key.startswith(str(bot.self_id) + ':')]
        for key in keys:
            # 每项独立串行，失败不会阻止其它群会话。
            async with self._lock(key):
                session = copy.deepcopy(self.store.data['sessions'].get(key))
                if not session or session['status'] not in ('pending', 'grace'):
                    continue
                gid = session['group_id']
                cfg = self._cfg(gid)
                if not cfg['enabled'] or not self.active(bot):
                    self._drop(key)
                    continue
                if session['status'] == 'pending':
                    warning = cfg.get('warning_time', self.repo.settings.verification.warning_time)
                    if self.now() >= session['deadline']:
                        await self._grace(bot, key, session, cfg)
                    elif warning > 0 and not session['warned'] and self.now() >= session['deadline'] - warning:
                        await self._warn(bot, key, session, cfg)
                elif self.now() >= session['kick_at']:
                    await self._kick(bot, key, session)

    async def _warn(self, bot, key, session, cfg):
        values = self._values(session['group_id'], session['user_id'], member_name=session['member_name'])
        text = cfg.get('countdown_warning_prompt', self.repo.settings.verification.countdown_warning_prompt)
        if session['method'] == 'combined':
            try:
                session['url'] = await self._backend().create(session['group_id'], session['user_id'])
                text += '\n' + session['url'] + '\n请完成网页验证后发送六位验证码。'
            except Exception:
                if not self._current(bot, key, session):
                    return
                member = await self._member(bot, session['group_id'], session['user_id'])
                if member:
                    await self._start(bot, session['group_id'], session['user_id'], member, cfg,
                                      wrong_count=session['wrong_count'], force_math=True, expected_session=session)
                else:
                    self._manual(key, session, '无法确认群成员状态')
                return
        if not self._current(bot, key, session):
            return
        session['warned'] = True
        self._put(key, session)
        if not await self._send(bot, session['group_id'], self._text(text, values), uid=session['user_id']):
            self._manual(key, session, '提醒未能送达，暂停自动处罚')

    async def _kick(self, bot, key, session):
        gid, uid = session['group_id'], session['user_id']
        member = await self._member(bot, gid, uid)
        own = await self._member(bot, gid, bot.self_id)
        if not member or not own:
            self._manual(key, session, '无法确认成员或机器人权限，暂停自动处罚')
            return
        if member.get('role') in ('owner', 'admin') or own.get('role') not in ('owner', 'admin'):
            self._manual(key, session, '角色权限不允许移出成员')
            return
        if not session.get('join_time') or member.get('join_time') != session['join_time']:
            self._manual(key, session, '入群批次发生变化，暂停自动处罚')
            return
        if not self._current(bot, key, session):
            return
        if self.store.data['bypass'].get(key):
            self._drop(key)
            return
        # 先提交不可重放状态；网络结果不明时不重复处罚。
        self._put(key, dict(session, status='kicking'))
        try:
            await bot.call_api('set_group_kick', group_id=gid, user_id=uid, reject_add_request=False)
        except Exception as error:
            self._manual(key, session, '移出操作失败：' + type(error).__name__)
            return
        if not self._current(bot, key, session):
            return
        def cleanup(data):
            data['sessions'].pop(key, None)
            data['records'].get(str(gid), {}).pop(str(uid), None)
        self.store.transaction(cleanup)
        text = self._text(self.repo.settings.verification.kick_message, self._values(gid, uid))
        await self._send(bot, gid, text, uid=uid)

    async def bypass(self, bot, gid, uid):
        key = self._key(bot.self_id, gid, uid)
        # 不等待网络锁，持久化取消标记必须先于旧处罚的下一个检查。
        def update(data):
            data['bypass'][key] = True
            data['sessions'].pop(key, None)
        self.store.transaction(update)

    async def disable_group(self, bot, gid):
        self.repo.update_group(gid, enabled=False)
        prefix = f'{bot.self_id}:{gid}:'
        def cancel(data):
            for key in list(data['sessions']):
                if key.startswith(prefix):
                    data['sessions'].pop(key)
        self.store.transaction(cancel)

    async def restore(self, bot):
        if not self.active(bot):
            return
        sessions = [(key, copy.deepcopy(value)) for key, value in self.store.data['sessions'].items()
                    if key.startswith(str(bot.self_id) + ':') and value['status'] in ('pending', 'grace', 'preparing')]
        for key, session in sessions:
            async with self._lock(key):
                if not self._current(bot, key, session) or not self._matches(key, session):
                    continue
                gid, uid = session['group_id'], session['user_id']
                cfg = self._cfg(gid)
                member = await self._member(bot, gid, uid)
                if not self._current(bot, key, session) or not self._matches(key, session):
                    continue
                if not cfg['enabled'] or not member or member.get('role') in ('admin', 'owner'):
                    self._drop(key)
                    continue
                await self._start(bot, gid, uid, member, cfg, wrong_count=session['wrong_count'], expected_session=session)

    async def reverify(self, bot, gid, uid):
        await self.handle_join(bot, SimpleNamespace(group_id=gid, user_id=uid, time=int(self.now())), force=True)

    async def close(self):
        for backend in self._backends.values():
            await backend.close()
        self._backends.clear()
