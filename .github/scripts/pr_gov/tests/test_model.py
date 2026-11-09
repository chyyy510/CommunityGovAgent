"""模型 API 请求、回复及错误处理测试，不访问真实模型。"""
import copy
import io
from email.message import Message
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl
from test_assessment import CONFIG
from src.model import resolve_model_config, invoke_model, ModelError, detect_runtime
from src.common import dumps
from src.model import Response
from src import model

def config():
    value = copy.deepcopy(CONFIG)
    value['runtime'] = 'ci'
    return value

ENV = dict(CI='true', VALUE_MODEL_API_KEY='synthetic-model-key')
def never(*args, **kwargs): raise AssertionError('不应调用模型')

class ModelTests(unittest.TestCase):
    def urllib_response(self, *, body=b'{}', status=200, headers=None):
        calls = []
        class FakeHTTPS(HTTPSHandler):
            def https_open(self, req):
                calls.append(req)
                message = Message()
                for key, value in (headers or {}).items(): message[key] = value
                response = addinfourl(io.BytesIO(body), message, req.full_url, status)
                response.msg = '模拟响应'
                return response
        mocked = patch.object(model, 'build_opener', side_effect=lambda *handlers:
                              build_opener(ProxyHandler({}), *handlers, FakeHTTPS()))
        self.addCleanup(mocked.stop)
        mocked.start()
        return calls

    def test_标准库请求正确发送模型数据且不跟随重定向(self):
        calls = self.urllib_response()
        response = model.request('https://model.invalid/chat', headers={'Authorization': 'Bearer synthetic'}, body=b'{"test":1}')
        self.assertEqual(response.body, b'{}')
        self.assertEqual(calls[0].get_method(), 'POST')
        self.assertEqual(calls[0].data, b'{"test":1}')
        self.assertEqual(calls[0].get_header('Accept-encoding'), 'identity')
        self.assertEqual(calls[0].get_header('Authorization'), 'Bearer synthetic')
        calls = self.urllib_response(status=302, headers={'Location': 'https://other.invalid/chat'})
        response = model.request('https://model.invalid/chat', headers={'Authorization': 'Bearer synthetic'}, body=b'{}')
        self.assertEqual(response.status, 302)
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.body, b'')

    def test_标准库拒绝超大压缩响应且HTTP错误不读取正文(self):
        for body, headers in ((b'12345', {}), (b'{}', {'Content-Length': '5'}),
                              (b'{}', {'Content-Encoding': 'gzip'})):
            self.urllib_response(body=body, headers=headers)
            with self.assertRaisesRegex(ModelError, 'model_invalid_response'):
                model.request('https://model.invalid/chat', max_bytes=4)
        self.urllib_response(status=401, body=b'private-marker')
        response = model.request('https://model.invalid/chat')
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body, b'')

    def test_标准库网络异常及超时返回稳定原因码(self):
        selected = resolve_model_config(config(), ENV)
        for error, code in ((URLError(TimeoutError('private-marker')), 'model_timeout'),
                            (TimeoutError('private-marker'), 'model_timeout'),
                            (URLError('private-marker'), 'model_unavailable')):
            with patch.object(model, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(ModelError, '^' + code + '$'):
                    invoke_model([], selected, env=ENV)

    def test_思考模式配置限制(self):
        for mode in (None, 'disabled', 'enabled'):
            c = config()
            if mode is not None: c['api']['thinking'] = mode
            selected = resolve_model_config(c, ENV)
            self.assertEqual(selected['api'].get('thinking'), mode)
        for invalid in (None, True, 'auto', {}, []):
            c = config(); c['api']['thinking'] = invalid
            with self.assertRaisesRegex(ModelError, 'model_invalid_config'): resolve_model_config(c, ENV)

    def test_JSON空白截断及无效结构不会建议合并(self):
        from test_assessment import sample
        from src.assessment import assess
        for content, finish in [('   ', 'stop'), ('{"schemaVersion":1', 'length'), ('{}', 'stop')]:
            def fetch(*args, **kwargs):
                return Response(200, dumps(dict(choices=[dict(finish_reason=finish,
                    message=dict(role='assistant', content=content))])).encode())
            result = assess(sample(), config=config(), env=ENV, fetch=fetch)
            self.assertEqual(result['status'], 'uncertain')
            self.assertEqual(result['reasons'][0]['code'], 'model_invalid_response')

    def test_可选空值沿用模型默认值(self):
        c = config(); c['timeoutMs'] = None; c['api']['maxOutputTokens'] = None
        self.assertEqual(resolve_model_config(c, ENV)['timeoutMs'], 20_000)

    def test_配置限制与运行环境(self):
        self.assertEqual(detect_runtime({'CI': 'maybe'}), 'unknown')
        for field, value, reason in [('runtime', 'local', 'model_runtime_mismatch'), ('allowExternalProcessing', False, 'model_processing_not_authorized'), ('mode', 'codex', 'model_invalid_config'), ('timeoutMs', 0, 'model_invalid_config')]:
            c = config(); c[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ModelError, reason): resolve_model_config(c, ENV)
        for endpoint in ['http://model.invalid', 'https://user:pass@model.invalid', 'https://model.invalid?key=x']:
            c = config(); c['api']['endpoint'] = endpoint
            with self.assertRaisesRegex(ModelError, 'model_invalid_config'): resolve_model_config(c, ENV)
        for api in (None, {}, []):
            c = config(); c['api'] = api
            with self.assertRaisesRegex(ModelError, 'model_invalid_config'): resolve_model_config(c, ENV)
        c = config(); del c['api']
        with self.assertRaisesRegex(ModelError, 'model_invalid_config'): resolve_model_config(c, ENV)

    def test_HTTP传输封装及脱敏(self):
        calls = []
        def fetch(url, **options):
            calls.append((url, options))
            return Response(200, dumps(dict(choices=[dict(finish_reason='stop', message=dict(role='assistant', content='{}'))])).encode())
        selected = resolve_model_config(config(), ENV)
        self.assertEqual(invoke_model([dict(role='user', content='synthetic')], selected, env=ENV, fetch=fetch), '{}')
        self.assertEqual(calls[0][1]['headers']['Authorization'], 'Bearer synthetic-model-key')
        with self.assertRaisesRegex(ModelError, 'model_missing_api_key'): invoke_model([], selected, env={}, fetch=never)
        with self.assertRaisesRegex(ModelError, 'model_api_error'): invoke_model([], selected, env=ENV, fetch=lambda *a, **k: Response(401, b'private-key'))
