"""群管策略的离线回归，不初始化 NoneBot。"""
import importlib.util
import random
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'src/nonebot_plugin_group_guard/policy.py'


def load_policy():
    if not PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location('_guard_policy_test', PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_config():
    package_name = '_guard_policy_config_tests'
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(PATH.parent)]
        sys.modules[package_name] = package
    return importlib.import_module(f'{package_name}.config')


P = load_policy()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(P, '群管策略模块尚未实现')
        self.cfg = SimpleNamespace(
            accept_categories=['GitHub:github,gubhib', 'B站:bilibili,b站'],
            reject_key=['广告'], allows=lambda gid: str(gid) == '100',
        )

    def test_denial_has_priority(self):
        d = P.decide_request(self.cfg, 100, 'github 广告')
        self.assertEqual(d.action, 'reject')
        self.assertEqual(d.keyword, '广告')

    def test_case_insensitive_category(self):
        d = P.decide_request(self.cfg, 100, '来自 GitHub')
        self.assertEqual((d.action, d.category), ('approve', 'GitHub'))

    def test_unknown_answer_stays_manual(self):
        self.assertEqual(P.decide_request(self.cfg, 100, '').action, 'manual')
        self.assertEqual(P.decide_request(self.cfg, 100, '朋友推荐').action, 'manual')

    def test_unconfigured_group_ignored(self):
        self.assertEqual(P.decide_request(self.cfg, 200, 'github').action, 'ignore')

    def test_empty_keywords_do_not_match_everything(self):
        self.cfg.accept_categories = ['空:', '来源：，，']
        self.cfg.reject_key = [' ', '']
        self.assertEqual(P.decide_request(self.cfg, 100, 'hello').action, 'manual')

    def test_fullwidth_separator(self):
        self.cfg.accept_categories = ['频道：甲，乙']
        self.assertEqual(P.decide_request(self.cfg, 100, '从乙过来').category, '频道')

    def test_decision_appends_metadata_without_changing_positional_fields(self):
        decision = P.Decision('approve', '旧来源', '关键词')
        self.assertEqual((decision.action, decision.category, decision.keyword),
                         ('approve', '旧来源', '关键词'))
        self.assertEqual(getattr(decision, 'rule', None), '')
        self.assertEqual(getattr(decision, 'method', None), '')

    def test_legacy_decisions_keep_categories_and_identify_method(self):
        for comment, action, category in [('GITHUB', 'approve', 'GitHub'),
                                           ('广告', 'reject', ''), ('其他', 'manual', '')]:
            with self.subTest(comment=comment):
                decision = P.decide_request(self.cfg, 100, comment)
                self.assertEqual((decision.action, decision.category), (action, category))
                self.assertEqual(getattr(decision, 'method', None), 'legacy')
                self.assertEqual(getattr(decision, 'rule', None), '')

    def test_request_answer_accepts_plain_text_and_one_clear_question_pair(self):
        self.assertTrue(callable(getattr(P, 'request_answer', None)), '尚未实现申请回答提取')
        for comment, expected in [
            ('  Alpha  ', 'Alpha'), ('朋友推荐', '朋友推荐'),
            ('问题：来自哪里？\n答案： GitHub ', 'GitHub'),
            (' 问题:请填写固定答案\r\n 答案: Alpha \r\n', 'Alpha'),
            ('问题：介绍自己\n可分多行\n答案：第一行\n第二行', '第一行\n第二行'),
        ]:
            with self.subTest(comment=comment):
                self.assertEqual(P.request_answer(comment), expected)

    def test_request_answer_ambiguous_labels_stay_manual(self):
        self.assertTrue(callable(getattr(P, 'request_answer', None)), '尚未实现申请回答提取')
        for comment in [
            None, '', '  ', '问题：GitHub', '问题：GitHub\n答案： ',
            '答案：GitHub', '答案：GitHub\n问题：来源',
            '问题：来源？ 答案：GitHub', '前言\n问题：来源\n答案：GitHub',
            '问题：一\n问题：二\n答案：GitHub',
            '问题：一\n答案：GitHub\n答案：其他',
            '问题：一\n答案：GitHub\n问题：二\n答案：其他',
            '问题：一\n回答：GitHub', '问题：\n答案：GitHub',
        ]:
            with self.subTest(comment=comment):
                self.assertIsNone(P.request_answer(comment))

    def test_request_answer_never_recursively_strips_answer_labels(self):
        self.assertTrue(callable(getattr(P, 'request_answer', None)), '尚未实现申请回答提取')
        self.assertEqual(P.request_answer('问题：固定答案\n答案：答案：Alpha'), '答案：Alpha')
        self.assertEqual(P.request_answer('问题：固定答案\n答案：问题：Alpha'), '问题：Alpha')
        self.assertEqual(P.request_answer('问题：固定答案\n答案：问题：Q 答案：Alpha'),
                         '问题：Q 答案：Alpha')

    def test_legacy_matches_real_answer_not_question(self):
        for comment in ['问题：来自 GitHub 吗？\n答案：朋友推荐',
                        '问题：不要发送广告\n答案：朋友推荐',
                        '问题：GitHub', '问题：广告']:
            with self.subTest(comment=comment):
                self.assertEqual(P.decide_request(self.cfg, 100, comment).action, 'manual')
        self.assertEqual(P.decide_request(self.cfg, 100,
                                         '问题：不要发广告\n答案：GitHub').action, 'approve')
        self.assertEqual(P.decide_request(self.cfg, 100,
                                         '问题：来源\n答案：GitHub 广告').action, 'reject')

    def test_rules_exact_matches_trimmed_casefolded_alternatives(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '固定答案', 'match': 'exact', 'values': ['Alpha', 'Straße']}]
        for comment, keyword in [('  ALPHA ', 'Alpha'), ('strasse', 'Straße'),
                                 ('问题：请输入 Alpha\n答案：Alpha', 'Alpha')]:
            with self.subTest(comment=comment):
                decision = P.decide_request(self.cfg, 100, comment)
                self.assertEqual(decision.action, 'approve')
                self.assertEqual(decision.keyword, keyword)
                self.assertEqual(getattr(decision, 'rule', None), '固定答案')
                self.assertEqual(getattr(decision, 'method', None), 'rules')
                self.assertEqual(decision.category, '')
        for comment in ['Alpha 其他', '问题：Alpha\n答案：其他', 'github', '其他']:
            with self.subTest(comment=comment):
                self.assertEqual(P.decide_request(self.cfg, 100, comment).action, 'manual')

    def test_rules_contains_respects_case_and_explicit_source(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '任意答案', 'match': 'contains',
                                  'values': ['GitHub'], 'case_sensitive': True, 'source': '代码平台'}]
        matched = P.decide_request(self.cfg, 100, '来自 GitHub 的链接')
        self.assertEqual((matched.action, matched.category), ('approve', '代码平台'))
        self.assertEqual(getattr(matched, 'rule', None), '任意答案')
        self.assertEqual(P.decide_request(self.cfg, 100, '来自 github').action, 'manual')
        self.cfg.accept_rules[0]['case_sensitive'] = False
        self.assertEqual(P.decide_request(self.cfg, 100, '来自 GITHUB').action, 'approve')

    def test_rules_exact_case_sensitive_never_matches_substrings(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '严格答案', 'match': 'exact',
                                  'values': [' Alpha '], 'case_sensitive': True}]
        self.assertEqual(P.decide_request(self.cfg, 100, ' Alpha ').action, 'approve')
        for comment in ['alpha', 'Alpha Beta', '答案：Alpha', '问题：问\n答案：答案：Alpha']:
            with self.subTest(comment=comment):
                self.assertEqual(P.decide_request(self.cfg, 100, comment).action, 'manual')

    def test_rules_rejection_precedes_any_accept_rule(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '答案', 'values': ['Alpha 广告']}]
        decision = P.decide_request(self.cfg, 100, '问题：Alpha\n答案：Alpha 广告')
        self.assertEqual(decision.action, 'reject')
        self.assertEqual(decision.keyword, '广告')
        self.assertEqual(getattr(decision, 'method', None), 'rules')
        self.assertEqual(getattr(decision, 'rule', None), '')
        self.assertEqual(decision.category, '')

    def test_rules_keep_first_match_and_never_infer_source(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [
            {'name': 'GitHub', 'match': 'contains', 'values': ['Alpha']},
            {'name': '第二条', 'values': ['Alpha'], 'source': '显式来源'},
        ]
        decision = P.decide_request(self.cfg, 100, 'Alpha')
        self.assertEqual(decision.action, 'approve')
        self.assertEqual(getattr(decision, 'rule', None), 'GitHub')
        self.assertEqual(decision.category, '')

    def test_manual_mode_never_approves_or_rejects(self):
        self.cfg.approval_mode = 'manual'
        self.cfg.accept_rules = [{'name': '答案', 'values': ['Alpha']}]
        for comment in ['github', '广告', 'Alpha', '问题：广告\n答案：github', '']:
            with self.subTest(comment=comment):
                decision = P.decide_request(self.cfg, 100, comment)
                self.assertEqual(decision.action, 'manual')
                self.assertEqual(getattr(decision, 'method', None), 'manual')
                self.assertEqual((decision.category, decision.keyword, decision.rule), ('', '', ''))
        self.assertEqual(P.decide_request(self.cfg, 200, '广告').action, 'ignore')

    def test_simple_namespace_group_rules_override_only_explicit_fields(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '全局', 'values': ['Alpha']}]
        self.cfg.allows = lambda gid: True
        self.cfg.group_rules = {
            '100': {'accept_rules': [{'name': '群级', 'values': ['Beta'], 'source': '渠道'}]},
            '200': {'approval_mode': 'manual'},
            '300': {'approval_mode': 'legacy', 'accept_categories': ['群旧:Gamma']},
            '400': {'reject_key': []},
        }
        decision = P.decide_request(self.cfg, 100, 'Beta')
        self.assertEqual((decision.action, decision.category, getattr(decision, 'rule', None)),
                         ('approve', '渠道', '群级'))
        self.assertEqual(P.decide_request(self.cfg, 100, 'Alpha').action, 'manual')
        self.assertEqual(P.decide_request(self.cfg, 100, '广告').action, 'reject')
        self.assertEqual(P.decide_request(self.cfg, 200, '广告').action, 'manual')
        self.assertEqual(P.decide_request(self.cfg, 300, 'Gamma').category, '群旧')
        self.assertEqual(P.decide_request(self.cfg, 400, '广告').action, 'manual')
        self.assertEqual(P.decide_request(self.cfg, 500, 'Alpha').action, 'approve')

    def test_policy_uses_for_group_when_available(self):
        approval = SimpleNamespace(allows=lambda gid: True, for_group=lambda gid: {
            'approval_mode': 'rules', 'reject_key': [],
            'accept_rules': [{'name': str(gid), 'values': ['Alpha'], 'source': '来源'}],
        })
        decision = P.decide_request(approval, 100, 'Alpha')
        self.assertEqual((decision.action, decision.category, getattr(decision, 'rule', None)),
                         ('approve', '来源', '100'))

    def test_real_approval_config_inherits_rules_without_enabling_statistics(self):
        config = load_config().ApprovalConfig(
            approval_mode='rules', source_statistics=False,
            accept_rules=[{'name': '全局答案', 'values': ['Alpha'], 'source': '全局渠道'}],
            reject_key=['广告'], divide_group={'block_method': 'blacklist'},
            group_rules={
                '100': {'accept_rules': [{'name': '群级答案', 'values': ['Beta']}]},
                '200': {'approval_mode': 'manual'},
                '300': {'approval_mode': 'legacy', 'accept_categories': ['旧来源:Gamma']},
            },
        )
        for gid, comment, expected in [
            (100, 'Beta', ('approve', '', '群级答案', 'rules')),
            (100, 'Alpha', ('manual', '', '', 'rules')),
            (100, '广告', ('reject', '', '', 'rules')),
            (200, '广告', ('manual', '', '', 'manual')),
            (300, 'Gamma', ('approve', '旧来源', '', 'legacy')),
            (400, 'Alpha', ('approve', '全局渠道', '全局答案', 'rules')),
        ]:
            with self.subTest(gid=gid, comment=comment):
                decision = P.decide_request(config, gid, comment)
                self.assertEqual((decision.action, decision.category, decision.rule, decision.method),
                                 expected)
                self.assertFalse(config.source_enabled(gid))

    def test_rules_accept_model_entries_in_legacy_namespace_shape(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [SimpleNamespace(name='模型规则', match='exact', values=['Alpha'],
                                                 case_sensitive=False, source=None)]
        decision = P.decide_request(self.cfg, 100, 'Alpha')
        self.assertEqual(decision.action, 'approve')
        self.assertEqual(getattr(decision, 'rule', None), '模型规则')
        self.assertEqual(decision.category, '')

    def test_rules_question_and_ambiguous_format_never_trigger_rejection(self):
        self.cfg.approval_mode = 'rules'
        self.cfg.accept_rules = [{'name': '答案', 'match': 'contains', 'values': ['Alpha']}]
        for comment in ['问题：Alpha 广告\n答案：其他',
                        '问题：Alpha 广告', '答案：Alpha 广告',
                        '问题：一\n答案：Alpha 广告\n答案：其他',
                        '问题：一\n答案：Alpha\n问题：二\n答案：广告']:
            with self.subTest(comment=comment):
                decision = P.decide_request(self.cfg, 100, comment)
                self.assertEqual(decision.action, 'manual')
                self.assertEqual(getattr(decision, 'method', None), 'rules')
        decision = P.decide_request(self.cfg, 100, '问题：广告\n答案：Alpha')
        self.assertEqual(decision.action, 'approve')

    def test_rules_empty_values_and_missing_optional_namespace_fields_do_not_match(self):
        approval = SimpleNamespace(allows=lambda gid: True, approval_mode='rules', accept_rules=[
            {'name': '空', 'match': 'contains', 'values': ['', '  ']},
        ])
        self.assertEqual(P.decide_request(approval, 100, '其他').action, 'manual')
        old = SimpleNamespace(allows=lambda gid: True)
        self.assertEqual(P.decide_request(old, 100, '其他').action, 'manual')

    def test_template_first_separator_only(self):
        self.assertEqual(P.template_for(['default:默认', '100：链接：https://example.test/x'], 100), '链接：https://example.test/x')

    def test_empty_template_can_disable_notification(self):
        self.assertEqual(P.template_for(['default:欢迎', '100:'], 100), '')

    def test_substitution_is_non_recursive(self):
        out = P.render_template('%comment% {user_id} %user_name% {unknown}', {
            'comment': '%user_name%', 'user_id': 200, 'user_name': '甲',
        })
        self.assertEqual(out, '%user_name% 200 甲 {unknown}')

    def test_math_answer_requires_full_plaintext(self):
        for bad in ['这是42', '42 7', '答案42', '42abc', '[CQ:at,qq=42]', '101', '-1', '']:
            self.assertIsNone(P.parse_answer(bad, 'math'), bad)
        self.assertEqual(P.parse_answer(' 42 ', 'math'), '42')
        self.assertEqual(P.parse_answer('0', 'math'), '0')

    def test_web_answer_keeps_leading_zeros(self):
        self.assertEqual(P.parse_answer('000042', 'combined'), '000042')
        self.assertEqual(P.parse_answer('aB12Cd', 'combined'), 'aB12Cd')
        for bad in ['aB12C', 'aB12Cd7', '我的码aB12Cd', 'aB12Cd x']:
            self.assertIsNone(P.parse_answer(bad, 'combined'))

    def test_generated_problems_are_correct_and_bounded(self):
        rng = random.Random(7)
        for _ in range(500):
            text, answer = P.generate_problem(rng)
            a, op, b, *_ = text.split()
            expected = int(a) + int(b) if op == '+' else int(a) - int(b)
            self.assertEqual(answer, expected)
            self.assertTrue(0 <= answer <= 100)

    def test_notice_targets_support_legacy_and_native(self):
        for value, expected in [
            ('origin', ('group', 100)), ('group:300', ('group', 300)),
            ('private:400', ('private', 400)),
            ('default:GroupMessage:300', ('group', 300)),
            ('default:FriendMessage:400', ('private', 400)),
        ]:
            self.assertEqual(P.parse_target(value, 100), expected)

    def test_bad_targets_rejected(self):
        for value in ['other:300', 'group:abc', 'private:-4', 'group:0', '']:
            with self.assertRaises(ValueError, msg=value):
                P.parse_target(value, 100)

    def test_qq_level_only_reads_level_field(self):
        self.assertEqual(P.qq_level({'QQLevel': '20'}), 20)
        self.assertEqual(P.qq_level({'data': {'qqLevel': 21}}), 21)
        self.assertIsNone(P.qq_level({'level': '群等级5'}))
        self.assertIsNone(P.qq_level({'qqLevel': True}))
        self.assertIsNone(P.qq_level({'qqLevel': -1}))


if __name__ == '__main__':
    unittest.main()
