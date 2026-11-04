"""不依赖机器人框架的审批与输入策略。"""
import random
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    action: str
    category: str = ''
    keyword: str = ''
    rule: str = ''
    method: str = ''


def split_entry(text):
    parts = re.split('[:：]', str(text), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ('', '')


def request_answer(comment) -> str | None:
    """只提取一组明确的问答；回答里的行内伪标签保留原文。"""
    text = str(comment or '').strip()
    if not text:
        return None
    label = r'(问题|答案)[^\S\r\n]*[:：][^\S\r\n]*'
    labels = list(re.finditer(r'^[^\S\r\n]*' + label, text, flags=re.MULTILINE))
    if not labels:
        return None if re.search(label, text) else text
    if (len(labels) != 2 or labels[0].start() != 0
            or labels[0][1] != '问题' or labels[1][1] != '答案'):
        return None
    question = text[labels[0].end():labels[1].start()].strip()
    answer = text[labels[1].end():].strip()
    if not question or not answer or re.search(label, question):
        return None
    return answer


def decide_request(approval, gid, comment):
    if not approval.allows(gid):
        return Decision('ignore')
    config = {name: getattr(approval, name, default) for name, default in (
        ('approval_mode', 'legacy'), ('reject_key', []),
        ('accept_categories', []), ('accept_rules', []),
    )}
    resolve = getattr(approval, 'for_group', None)
    if callable(resolve):
        config.update(resolve(gid))
    else:
        config.update(getattr(approval, 'group_rules', {}).get(str(gid), {}))
    mode = config['approval_mode']
    if mode not in {'legacy', 'rules'}:
        return Decision('manual', method='manual')
    answer = request_answer(comment)
    if answer is None:
        return Decision('manual', method=mode)
    lowered = answer.casefold()
    for keyword in config['reject_key']:
        keyword = str(keyword).strip()
        if keyword and keyword.casefold() in lowered:
            return Decision('reject', keyword=keyword, method=mode)
    if mode == 'rules':
        for rule in config['accept_rules']:
            if not isinstance(rule, dict):
                rule = vars(rule)
            case_sensitive = rule.get('case_sensitive', False)
            candidate = answer if case_sensitive else lowered
            match = rule.get('match', 'exact')
            for value in rule.get('values', []):
                keyword = str(value).strip()
                if not keyword:
                    continue
                expected = keyword if case_sensitive else keyword.casefold()
                if ((match == 'exact' and candidate == expected)
                        or (match == 'contains' and expected in candidate)):
                    return Decision('approve', rule.get('source') or '', keyword,
                                    rule=rule['name'], method='rules')
        return Decision('manual', method='rules')
    for entry in config['accept_categories']:
        category, raw_keywords = split_entry(entry)
        if not category:
            continue
        for keyword in re.split('[,，]', raw_keywords):
            keyword = keyword.strip()
            if keyword and keyword.casefold() in lowered:
                return Decision('approve', category, keyword, method='legacy')
    return Decision('manual', method='legacy')


def parse_answer(text, method):
    text = str(text).strip()
    if method == 'math':
        if re.fullmatch(r'[0-9]{1,3}', text) and int(text) <= 100:
            return str(int(text))
        return None
    return text if re.fullmatch(r'[A-Za-z0-9]{6}', text) else None


def generate_problem(rng=None):
    rng = rng or random.SystemRandom()
    a = rng.randint(0, 100)
    if rng.choice((True, False)):
        b = rng.randint(0, 100 - a)
        return f'{a} + {b} = ?', a + b
    b = rng.randint(0, a)
    return f'{a} - {b} = ?', a - b


def template_for(entries, gid, default=''):
    templates = {}
    for entry in entries:
        key, value = split_entry(entry)
        if key:
            templates[key] = value
    return templates.get(str(gid), templates.get('default', default))


def render_template(text, values):
    # 单次替换，用户输入中的占位符不会被再次解释。
    pattern = r'%([A-Za-z_][A-Za-z_0-9]*)%|\{([A-Za-z_][A-Za-z_0-9]*)\}'
    return re.sub(pattern, lambda m: str(values.get(m[1] or m[2], m[0])), str(text))


def parse_target(value, origin_gid):
    if value == 'origin':
        return 'group', int(origin_gid)
    parts = str(value).split(':')
    kind = parts[-2] if len(parts) >= 2 else ''
    aliases = {'GroupMessage': 'group', 'FriendMessage': 'private', 'group': 'group', 'private': 'private'}
    identifier = parts[-1]
    if kind not in aliases or not re.fullmatch(r'[1-9][0-9]*', identifier):
        raise ValueError('通知目标需为 origin、group:群号或 private:QQ号')
    return aliases[kind], int(identifier)


def qq_level(info):
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if str(key).lower() == 'qqlevel' and not isinstance(value, bool):
            if re.fullmatch(r'[0-9]{1,3}', str(value)):
                number = int(value)
                return number if 0 <= number <= 256 else None
    return qq_level(info.get('data'))
