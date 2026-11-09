"""通过模拟 HTTP 响应覆盖重试、诊断、契约和分批，不调用真实模型。"""
import copy
import json
import unittest
from unittest.mock import Mock

from test_assessment import CONFIG, reply, sample
from src.assessment import assess
from src.comments import render_comment
from src.inspection import inspect_input
from src.model import Response


class StabilityTests(unittest.TestCase):
    def run_responses(self, responses, *, data=None, config=None):
        calls = []

        def fetch(url, **options):
            payload = json.loads(options['body'])
            calls.append(payload)
            response = responses.pop(0)
            if isinstance(response, Exception): raise response
            if isinstance(response, Response): return response
            content, finish = response if isinstance(response, tuple) else (response, 'stop')
            if callable(content): content = content(payload['messages'], None)
            return Response(200, json.dumps(dict(choices=[dict(finish_reason=finish,
                message=dict(role='assistant', content=content))])).encode())

        with self.assertLogs('src.assessment', level='WARNING') as logs:
            result = assess(data or sample(), config=config or CONFIG,
                env={'VALUE_MODEL_API_KEY': 'synthetic-secret'}, fetch=fetch)
        diagnostics = [json.loads(record.getMessage().split('：', 1)[1]) for record in logs.records]
        return result, calls, diagnostics

    def assert_unavailable(self, result):
        self.assertEqual(result['modelReview']['status'], 'unavailable')
        self.assertEqual(result['evidence'], [])
        self.assertNotEqual(result['status'], 'candidate')
        self.assertNotIn('**建议合并**', render_comment(result))

    def test_分类或审阅无效JSON仅重试一次后成功(self):
        for responses, stage in ((['{', reply(), reply()], 'classification'),
                                 ([reply(), '{', reply()], 'review')):
            result, calls, logs = self.run_responses(responses)
            self.assertEqual(result['status'], 'candidate')
            self.assertEqual(len(calls), 3)
            self.assertEqual(logs[0], dict(stage=stage, batch=1 if stage == 'review' else None,
                attempt=1, model='test', reason='invalid_json', code='model_invalid_response',
                http_status=200, finish_reason='stop'))
            retry_index = 1 if stage == 'classification' else 2
            self.assertEqual(calls[retry_index]['messages'][1], calls[retry_index - 1]['messages'][1])

    def test_连续两次失败保守终止且日志不包含原文或密钥(self):
        for responses, stage in ((['private-response', '{}'], 'classification'),
                                 ([reply(), 'private-response', '{}'], 'review')):
            result, calls, logs = self.run_responses(responses, data=sample(body='private-pr-content'))
            self.assert_unavailable(result)
            self.assertEqual([r['attempt'] for r in logs], [1, 2])
            self.assertEqual([r['reason'] for r in logs], ['invalid_json', 'invalid_schema'])
            self.assertTrue(all(r['stage'] == stage for r in logs))
            self.assertEqual(len(calls), 2 if stage == 'classification' else 3)
            if stage == 'classification': self.assertNotIn('classification', result)
            combined = json.dumps(logs) + render_comment(result)
            for marker in ('private-response', 'private-pr-content', 'synthetic-secret', 'Authorization'):
                self.assertNotIn(marker, combined)
            self.assertNotIn('invalid_json', render_comment(result))

    def test_空回复和API错误分别记录且有界重试(self):
        for response, reason, status, finish in (
            ('   ', 'empty_response', 200, 'stop'), (None, 'empty_response', 200, 'stop'),
            (Response(200, b''), 'empty_response', 200, None),
            (Response(200, b'{'), 'invalid_json', 200, None),
            (Response(503, b'private-response'), 'api_error', 503, None),
            (TimeoutError('private-response'), 'api_error', None, None),
            (('{}', 'private-response'), 'invalid_schema', 200, 'unknown')):
            with self.subTest(reason=reason, status=status, finish=finish):
                result, calls, logs = self.run_responses([response, response])
                self.assert_unavailable(result)
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(r['reason'] == reason and r['http_status'] == status and
                    r['finish_reason'] == finish for r in logs))
                self.assertNotIn('private-response', json.dumps(logs))

    def test_截断后重发完整JSON且恢复不超过token上限(self):
        for tokens in (2048, 8192):
            config = copy.deepcopy(CONFIG)
            config['api'].update(maxOutputTokens=tokens, thinking='disabled')
            result, calls, logs = self.run_responses([reply(), ('{', 'length'), reply()], config=config)
            self.assertEqual(result['status'], 'candidate')
            self.assertEqual(logs[0]['reason'], 'truncated_response')
            self.assertEqual(logs[0]['finish_reason'], 'length')
            self.assertEqual(calls[2]['max_tokens'], min(8192, tokens * 2))
            self.assertEqual(calls[1]['max_tokens'], tokens)
            self.assertIn('上次输出被截断', calls[2]['messages'][0]['content'])
            for call in calls:
                self.assertEqual(call['response_format'], {'type': 'json_object'})
                self.assertEqual(call['temperature'], 0)
                self.assertEqual(call['thinking'], {'type': 'disabled'})
        result, calls, logs = self.run_responses([('{', 'length'), ('{}', 'length')])
        self.assert_unavailable(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r['reason'] for r in logs], ['truncated_response'] * 2)

    def test_思考模式不传temperature(self):
        config = copy.deepcopy(CONFIG)
        config['api']['thinking'] = 'enabled'
        result, calls, _ = self.run_responses(['{', reply(), reply()], config=config)
        self.assertEqual(result['status'], 'candidate')
        self.assertTrue(all('temperature' not in c for c in calls))

    def test_遗漏重复未知ID及错误引文不能被接受(self):
        data = sample()
        data['files'].extend(sample(path='docs/second.md')['files'])
        data['source'] = dict(baseSha='a' * 40, headSha='b' * 40)
        data['references'] = [dict(id='config', kind='configuration', content='timeout = 60',
            source=dict(trust='adapter_verified', provider='local', revision='b' * 40,
                        path='config.py', startLine=1))]
        mutations = [
            ('missing_or_duplicate_change', lambda r: r['assessments'].pop()),
            ('missing_or_duplicate_change', lambda r: r['assessments'][1].update(id='change-1')),
            ('missing_or_duplicate_change', lambda r: r['assessments'][1].update(id='unknown')),
            ('invalid_reference', lambda r: r['assessments'][0].update(references=[dict(id='missing', quote='timeout = 60')])),
            ('invalid_reference', lambda r: r['assessments'][0].update(references=[dict(id='config', quote='timeout = 30')])),
            ('invalid_schema', lambda r: r['assessments'][0].update(before='伪造原文')),
        ]
        for reason, mutate in mutations:
            with self.subTest(reason=reason):
                invalid = reply(mutate=mutate)
                result, calls, logs = self.run_responses([reply(), invalid, invalid], data=data)
                self.assert_unavailable(result)
                self.assertEqual(len(calls), 3)
                self.assertEqual([r['reason'] for r in logs], [reason, reason])
                result, _, _ = self.run_responses([reply(), invalid, reply()], data=data)
                self.assertEqual(result['modelReview']['status'], 'completed')

    def test_批次失败不发布已成功的部分且恢复只重试当前批次(self):
        data = sample()
        data['files'] = [sample(before=f'Bad grammer {i}.', after=f'Bad grammar {i}.',
                                path=f'docs/{i}.md')['files'][0] for i in range(11)]
        for final in ('{', reply(mutate=lambda r: r['assessments'].reverse())):
            responses = [reply(), reply(), '{', final]
            if callable(final): responses.append(reply())
            result, calls, logs = self.run_responses(responses, data=data)
            self.assertEqual(logs[0]['batch'], 2)
            self.assertEqual(calls[2]['messages'][1], calls[3]['messages'][1])
            if not callable(final):
                self.assert_unavailable(result)
                self.assertEqual(len(calls), 4)
            else:
                original = inspect_input(data)['changes']
                self.assertEqual(result['status'], 'candidate')
                self.assertEqual(len(calls), 5)
                self.assertEqual([(e['id'], e['before'], e['after']) for e in result['evidence']],
                                 [(c['id'], c['before'], c['after']) for c in original])

    def test_范围外分类保持跳过且不触发审阅(self):
        invoke = Mock(wraps=reply(classification='other'))
        result = assess(sample(title='docs: fix API behavior', body='Update configuration defaults and add a new tutorial.'),
                        config=CONFIG, env={}, invoke=invoke)
        invoke.assert_called_once()
        self.assertEqual(result['screening'], 'skipped')
        self.assertEqual(result['modelReview']['status'], 'skipped')
