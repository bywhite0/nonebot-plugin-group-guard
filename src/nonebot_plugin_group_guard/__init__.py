"""关键词审批、来源统计与入群验证的 NoneBot 入口。"""
import asyncio
import json
import re
from pathlib import Path

from nonebot import get_driver, get_plugin_config, on_command, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent, GroupMessageEvent,
    GroupRequestEvent, Message,
)
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from .config import Config, ConfigRepository
from .messages import to_message
from .policy import split_entry
from .service import GuardService
from .store import StateStore

__plugin_meta__ = PluginMetadata(
    name='群管整合', description='关键词审批、来源统计和极验/算术入群验证',
    usage='/群管帮助 查看命令；默认关闭，先停旧端群管再开启。',
    type='application', config=Config, supported_adapters={'~onebot.v11'},
    extra={'license': 'AGPL-3.0', 'version': '1.0.1'},
)
config = get_plugin_config(Config)
# 默认数据相对应用启动目录，安装到 site-packages 后也不向包目录写入。


def absolute(path):
    return Path(path).expanduser().resolve()

repo = ConfigRepository(absolute(config.group_guard_config_file))
repo.load()
store = StateStore(absolute(config.group_guard_state_file))
store.load()
service = GuardService(repo, store)
driver = get_driver()
workers = {}


def request_rule(bot: Bot, event: GroupRequestEvent) -> bool:
    return service.active(bot) and event.sub_type == 'add' and repo.settings.approval.allows(event.group_id)


def notice_rule(bot: Bot, event: GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent) -> bool:
    return service.enabled_group(bot, event.group_id)


def answer_rule(bot: Bot, event: GroupMessageEvent) -> bool:
    return (service.active(bot) and event.user_id != int(bot.self_id)
            and repo.settings.verification.for_group(event.group_id)['enabled']
            and service.pending(bot.self_id, event.group_id, event.user_id) is not None)


request_matcher = on_request(rule=request_rule, priority=3, block=False)
notice_matcher = on_notice(rule=notice_rule, priority=3, block=False)
answer_matcher = on_message(rule=answer_rule, priority=3, block=False)


@request_matcher.handle()
async def request_handler(bot: Bot, event: GroupRequestEvent):
    await service.handle_request(bot, event)


@notice_matcher.handle()
async def notice_handler(bot: Bot, event: GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent):
    if isinstance(event, GroupIncreaseNoticeEvent):
        await service.handle_join(bot, event)
    else:
        await service.handle_leave(bot, event)


@answer_matcher.handle()
async def answer_handler(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    if await service.handle_answer(bot, event):
        matcher.stop_propagation()


async def can_manage(bot: Bot, event: GroupMessageEvent) -> bool:
    return await SUPERUSER(bot, event) or event.sender.role in ('owner', 'admin')


async def require_manager(matcher, bot, event):
    if not await can_manage(bot, event):
        await matcher.finish('只有群主、管理员或 Bot 超级用户有权限操作。')


def extract_target(args: Message):
    targets = [str(seg.data.get('qq', '')) for seg in args if seg.type == 'at']
    text = args.extract_plain_text().strip()
    if targets:
        return int(targets[0]) if len(targets) == 1 and targets[0].isdigit() and not text else None
    return int(text) if re.fullmatch(r'[1-9][0-9]{0,15}', text) else None


master_cmd = on_command('群管总开关', priority=6, block=True)
help_cmd = on_command('群管帮助', priority=6, block=True)
stats_cmd = on_command('入群统计', aliases={'加群统计'}, priority=6, block=True)
show_cmd = on_command('查看验证配置', aliases={'群管配置'}, priority=6, block=True)
enable_cmd = on_command('开启验证', priority=6, block=True)
disable_cmd = on_command('关闭验证', priority=6, block=True)
reverify_cmd = on_command('重新验证', aliases={'reverify'}, priority=6, block=True)
bypass_cmd = on_command('绕过验证', priority=6, block=True)
unbypass_cmd = on_command('取消绕过验证', priority=6, block=True)
timeout_cmd = on_command('设置验证超时时间', priority=6, block=True)
level_on_cmd = on_command('开启等级验证', priority=6, block=True)
level_off_cmd = on_command('关闭等级验证', priority=6, block=True)
level_cmd = on_command('设置最低验证等级', priority=6, block=True)
web_on_cmd = on_command('开启网页验证', priority=6, block=True)
web_off_cmd = on_command('关闭网页验证', priority=6, block=True)
limit_cmd = on_command('设置验证错误次数', priority=6, block=True)
kick_delay_cmd = on_command('设置踢出延迟', priority=6, block=True)
keyword_cmd = on_command('群管关键词', priority=6, block=True)
approval_cmd = on_command('群管审批', priority=6, block=True)
pending_cmd = on_command('待验证列表', priority=6, block=True)
mode_cmd = on_command('群管审批模式', priority=6, block=True)
rules_cmd = on_command('群管规则', priority=6, block=True)
source_cmd = on_command('群管来源统计', priority=6, block=True)


@master_cmd.handle()
async def master_switch(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    if not await SUPERUSER(bot, event):
        await matcher.finish('只有 Bot 超级用户有权限切换群管总开关。')
    value = args.extract_plain_text().strip()
    if value not in ('开启', '关闭', 'on', 'off'):
        await matcher.finish('/群管总开关 开启|关闭；请先停用 AstrBot 对应群管，避免重复执行。')
    enabled = value in ('开启', 'on')
    repo.set_enabled(enabled)
    if enabled:
        await service.restore(bot)
    else:
        # 开关关闭后所有事件静默，待验状态转人工，避免下次开启追罚。
        def cancel(data):
            for state in data['sessions'].values():
                if state['self_id'] == str(bot.self_id):
                    state['status'] = 'manual'
                    state['reason'] = '总开关已关闭'
        store.transaction(cancel)
    await matcher.finish('群管总开关已' + ('开启。' if enabled else '关闭。'))


@help_cmd.handle()
async def help_handler(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await require_manager(matcher, bot, event)
    await matcher.finish(
        '群管整合命令\n'
        '/群管总开关 开启|关闭（仅超管）\n'
        '/群管审批 开启|关闭\n'
        '/群管审批模式 兼容|规则|人工\n'
        '/群管规则 查看|添加 JSON|删除 规则名\n'
        '/群管来源统计 开启|关闭|默认\n'
        '/群管关键词 查看\n'
        '/群管关键词 允许添加 来源分类:关键词1,关键词2\n'
        '/群管关键词 允许删除 来源分类\n'
        '/群管关键词 拒绝添加|拒绝删除 关键词\n'
        '/开启验证 /关闭验证 /查看验证配置 /待验证列表\n'
        '/重新验证 @成员 /绕过验证 @成员 /取消绕过验证 @成员\n'
        '/设置验证超时时间 秒 /设置验证错误次数 次 /设置踢出延迟 秒\n'
        '/开启等级验证 /关闭等级验证 /设置最低验证等级 等级\n'
        '/开启网页验证 /关闭网页验证 /入群统计\n'
        '高级模板和通知目标在 data/group_guard/config.json；修改后重启。\n'
        '来源与许可：插件目录 README.md、NOTICE.md、LICENSE。'
    )


@show_cmd.handle()
async def verification_settings(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    cfg = repo.settings.verification.for_group(event.group_id)
    approval = repo.settings.approval
    mode = approval.for_group(event.group_id)['approval_mode']
    await matcher.finish(
        f"总开关：{'开' if repo.settings.enabled else '关'}\n"
        f"本群申请审批：{'开' if approval.allows(event.group_id) else '关'}；模式 {mode}\n"
        f"本群来源统计：{'开' if approval.chart_enabled(event.group_id) else '关'}\n"
        f"本群验证：{'开' if cfg['enabled'] else '关'}\n"
        f"网页验证：{'开' if cfg['enable_combined_verify'] else '关'}；失败回退算术题\n"
        f"等级免验：{'开' if cfg['enable_level_verify'] else '关'}，门槛 {cfg['min_qq_level']}\n"
        f"超时：{cfg['verification_timeout']} 秒；错误上限：{cfg['max_wrong_answers']}\n"
        f"踢出延迟：{cfg.get('kick_delay', repo.settings.verification.kick_delay)} 秒\n"
        '配置不会展示 API 密钥。'
    )


@enable_cmd.handle()
async def enable_verification(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await require_manager(matcher, bot, event)
    member = await service._member(bot, event.group_id, bot.self_id)
    if not member or member.get('role') not in ('owner', 'admin'):
        await matcher.finish('请先将机器人设为本群管理员，再开启验证。')
    repo.update_group(event.group_id, enabled=True)
    await matcher.finish('本群入群验证已开启；总开关' + ('已开。' if repo.settings.enabled else '仍关闭，暂不执行。'))


@disable_cmd.handle()
async def disable_verification(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await require_manager(matcher, bot, event)
    await service.disable_group(bot, event.group_id)
    await matcher.finish('本群验证已关闭，待验证处罚已取消。')


@reverify_cmd.handle()
async def reverify_member(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    target = extract_target(args)
    if target is None:
        await matcher.finish('/重新验证 @成员 或 /重新验证 QQ号；每次只指定一人。')
    if not service.active(bot) or not repo.settings.verification.for_group(event.group_id)['enabled']:
        await matcher.finish('请先开启总开关和本群验证。')
    if target == int(bot.self_id):
        await matcher.finish('不能验证机器人自身。')
    await service.reverify(bot, event.group_id, target)
    if service.pending(bot.self_id, event.group_id, target) is None:
        await matcher.finish('目标不存在、角色不适用或验证未能启动，未执行处罚。')
    await matcher.finish('已重新发起验证。')


@bypass_cmd.handle()
async def bypass_member(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    target = extract_target(args)
    if target is None:
        await matcher.finish('/绕过验证 @成员 或 /绕过验证 QQ号。')
    await service.bypass(bot, event.group_id, target)
    await matcher.finish('已取消当前验证并加入本群持久免验名单。')


@unbypass_cmd.handle()
async def unbypass_member(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    target = extract_target(args)
    if target is None:
        await matcher.finish('/取消绕过验证 @成员 或 /取消绕过验证 QQ号。')
    key = service._key(bot.self_id, event.group_id, target)
    async with service._lock(key):
        store.transaction(lambda data: data['bypass'].pop(key, None))
    await matcher.finish('已移出本群免验名单；本次不主动重验。')


async def set_number(matcher, bot, event, args, field, minimum, maximum):
    await require_manager(matcher, bot, event)
    text = args.extract_plain_text().strip()
    if not re.fullmatch(r'[0-9]{1,7}', text) or not minimum <= int(text) <= maximum:
        await matcher.finish(f'请输入 {minimum}～{maximum} 之间的整数。')
    repo.update_group(event.group_id, **{field: int(text)})
    await matcher.finish('已更新本群配置；时间与次数参数对下一轮验证生效。')


@timeout_cmd.handle()
async def set_timeout(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await set_number(matcher, bot, event, args, 'verification_timeout', 30, 86400)


@level_cmd.handle()
async def set_level(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await set_number(matcher, bot, event, args, 'min_qq_level', 0, 256)


@limit_cmd.handle()
async def set_limit(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await set_number(matcher, bot, event, args, 'max_wrong_answers', 1, 100)


@kick_delay_cmd.handle()
async def set_kick_delay(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await set_number(matcher, bot, event, args, 'kick_delay', 0, 300)


async def toggle_option(matcher, bot, event, field, enabled):
    await require_manager(matcher, bot, event)
    repo.update_group(event.group_id, **{field: enabled})
    await matcher.finish('已更新本群设置，下一轮验证生效。')


@level_on_cmd.handle()
async def enable_level(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await toggle_option(matcher, bot, event, 'enable_level_verify', True)


@level_off_cmd.handle()
async def disable_level(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await toggle_option(matcher, bot, event, 'enable_level_verify', False)


@web_on_cmd.handle()
async def enable_web(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await toggle_option(matcher, bot, event, 'enable_combined_verify', True)


@web_off_cmd.handle()
async def disable_web(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await toggle_option(matcher, bot, event, 'enable_combined_verify', False)


@keyword_cmd.handle()
async def manage_keywords(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    parts = args.extract_plain_text().strip().split(maxsplit=1)
    action, value = (parts + [''])[:2] if parts else ('查看', '')
    settings = repo.settings.model_copy(deep=True)
    approval = settings.approval
    gid = str(event.group_id)
    overrides = approval.group_rules.get(gid, {})
    allowed = list(overrides.get('accept_categories', approval.accept_categories))
    rejected = list(overrides.get('reject_key', approval.reject_key))
    if action == '查看':
        await matcher.finish('本群允许分类：\n' + '\n'.join(allowed) + '\n拒绝关键词：\n' + '\n'.join(rejected))
    if not value:
        await matcher.finish('/群管关键词 允许添加 分类:词1,词2 | 允许删除 分类 | 拒绝添加 词 | 拒绝删除 词')
    if action == '允许添加':
        category, words = split_entry(value)
        if not category or not any(x.strip() for x in re.split('[,，]', words)):
            await matcher.finish('格式：/群管关键词 允许添加 来源分类:关键词1,关键词2')
        allowed = [x for x in allowed if split_entry(x)[0] != category] + [category + ':' + words]
    elif action == '允许删除':
        allowed = [x for x in allowed if split_entry(x)[0] != value]
    elif action == '拒绝添加':
        if value not in rejected:
            rejected.append(value)
    elif action == '拒绝删除':
        rejected = [x for x in rejected if x != value]
    else:
        await matcher.finish('不支持的操作，请使用 /群管帮助。')
    approval.group_rules[gid] = {**overrides, 'accept_categories': allowed, 'reject_key': rejected}
    repo.save(settings)
    await matcher.finish('已更新本群关键词，不影响其它群。')


@mode_cmd.handle()
async def set_approval_mode(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    modes = {'兼容': 'legacy', '规则': 'rules', '人工': 'manual',
             'legacy': 'legacy', 'rules': 'rules', 'manual': 'manual'}
    value = args.extract_plain_text().strip()
    if value not in modes:
        await matcher.finish('/群管审批模式 兼容|规则|人工；仅修改当前群。')
    repo.update_approval_group(event.group_id, approval_mode=modes[value])
    await matcher.finish('本群审批模式已更新；来源统计单独配置，未匹配答案留人工。')


@source_cmd.handle()
async def set_source_statistics(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    options = {'开启': True, '关闭': False, '默认': None}
    value = args.extract_plain_text().strip()
    if value not in options:
        await matcher.finish('/群管来源统计 开启|关闭|默认；默认仅兼容模式启用。')
    repo.update_approval_group(event.group_id, source_statistics=options[value])
    await matcher.finish('本群来源统计设置已更新，不影响审批与人机验证。')


@rules_cmd.handle()
async def manage_approval_rules(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    parts = args.extract_plain_text().strip().split(maxsplit=1)
    action, value = (parts + [''])[:2] if parts else ('查看', '')
    config = repo.settings.approval.for_group(event.group_id)
    rules = list(config['accept_rules'])
    if action == '查看':
        rows = [f"{rule['name']}：{rule['match']}，{len(rule['values'])} 个允许答案，来源 {rule.get('source') or '不映射'}"
                for rule in rules]
        await matcher.finish('审批模式：' + config['approval_mode'] + '\n' + ('\n'.join(rows) or '尚未配置允许规则。')
                             + '\n答案不在群内展示；规则模式才使用此列表。')
    if not value:
        await matcher.finish('/群管规则 添加 {"name":"知识题","match":"exact","values":["答案甲","答案乙"]} | 删除 规则名')
    try:
        if action == '添加':
            rule = json.loads(value)
            if not isinstance(rule, dict) or not isinstance(rule.get('name'), str):
                raise ValueError('规则必须是带名称的对象')
            rules = [entry for entry in rules if entry['name'] != rule['name'].strip()] + [rule]
        elif action == '删除':
            rules = [entry for entry in rules if entry['name'] != value]
        else:
            await matcher.finish('不支持的操作，请使用 /群管帮助。')
        repo.update_approval_group(event.group_id, accept_rules=rules)
    except (ValueError, TypeError):
        await matcher.finish('规则无效：名称与答案不能为空，match 只支持 exact/contains；配置未修改。')
    await matcher.finish('已保存本群允许规则；需 /群管审批模式 规则 后使用，未改其它群。')


@approval_cmd.handle()
async def approval_switch(matcher: Matcher, bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    await require_manager(matcher, bot, event)
    value = args.extract_plain_text().strip()
    if value not in ('开启', '关闭'):
        await matcher.finish('/群管审批 开启|关闭，仅修改当前群。')
    settings = repo.settings.model_copy(deep=True)
    scope = settings.approval.divide_group
    group = str(event.group_id)
    enabled = value == '开启'
    controls = [str(x) for x in scope.get('control_list', [])]
    if (scope.get('block_method', 'whitelist') == 'whitelist') == enabled:
        if group not in controls:
            controls.append(group)
    else:
        controls = [x for x in controls if x != group]
    scope['control_list'] = controls
    repo.save(settings)
    await matcher.finish('本群申请审批已' + value + '。')


@pending_cmd.handle()
async def show_pending(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    await require_manager(matcher, bot, event)
    rows = []
    for state in store.data['sessions'].values():
        if (state['self_id'] == str(bot.self_id) and state['group_id'] == event.group_id
                and state['status'] != 'verified'):
            rows.append(f"{state['user_id']}：{state['status']}，错误 {state['wrong_count']} 次")
    await matcher.finish('\n'.join(rows[:100]) if rows else '本群没有待处理的验证。')


@stats_cmd.handle()
async def show_statistics(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    if not service.active(bot) or not repo.settings.approval.chart_enabled(event.group_id):
        await matcher.finish('本群来源统计未启用。')
    image = await service._chart(event.group_id)
    if not image:
        await matcher.finish('本群暂无来源记录，或已关闭统计图。')
    await matcher.finish(to_message('', image=image))


async def watch(bot):
    try:
        await service.restore(bot)
        while True:
            await asyncio.sleep(2)
            try:
                await service.tick(bot)
            except Exception as error:
                logger.warning('群管周期检查失败：{}', type(error).__name__)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.error('群管恢复任务失败：{}，已停止自动处罚。', type(error).__name__)


@driver.on_bot_connect
async def connected(bot: Bot):
    if not isinstance(bot, Bot):
        return
    old = workers.pop(bot.self_id, None)
    if old:
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
    workers[bot.self_id] = asyncio.create_task(watch(bot))


@driver.on_bot_disconnect
async def disconnected(bot: Bot):
    old = workers.pop(bot.self_id, None)
    if old:
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@driver.on_shutdown
async def shutdown():
    tasks = list(workers.values())
    workers.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await service.close()
