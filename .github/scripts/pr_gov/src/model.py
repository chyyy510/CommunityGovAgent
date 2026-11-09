"""调用模型 API 并检查回复；调用失败时返回错误代码，由上层生成中文提示。"""

import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .common import obj, integer, rx, dumps, loads, read_json_file

MAX_MODEL_REQUEST_BYTES = 48_000


class ModelError(Exception):
    # 只保存程序定义的错误代码，不把模型接口的原始错误信息带入评论或日志。
    def __init__(self, code, *, reason=None, http_status=None, finish_reason=None):
        super().__init__(code)
        self.reason = reason or ('invalid_schema' if code == 'model_invalid_response' else 'api_error')
        self.http_status = http_status
        self.finish_reason = finish_reason

    @property
    def code(self): return str(self)


def fail(code, **details): raise ModelError(code, **details)


class ModelText(str):
    # 保持字符串接口兼容，同时让后续契约校验失败时仍能记录响应状态。
    def __new__(cls, text, http_status, finish_reason):
        value = super().__new__(cls, text)
        value.http_status = http_status
        value.finish_reason = finish_reason
        return value


@dataclass
class Response:
    # 统一真实请求和测试模拟返回的数据格式。
    status: int
    body: bytes = b''

    @property
    def ok(self): return 200 <= self.status < 300


def valid_https(url):
    # 密钥放在请求头中；接口地址不能包含账号密码、? 参数或 # 后缀。
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url): return False

    try:
        parts = urlsplit(url)
        return parts.scheme == 'https' and bool(parts.hostname) and parts.username is None and parts.password is None and '?' not in url and '#' not in url and bool(parts.port or 443)
    except ValueError: return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 模型密钥不随重定向转发到另一个地址。
        return None


def request(url, *, method='POST', headers=None, body=None, timeout=60, max_bytes=64_000):
    if not valid_https(url): fail('model_invalid_config')
    req = Request(url, data=body, headers={**(headers or {}), 'Accept-Encoding': 'identity'}, method=method)

    try:
        # urllib 负责 HTTPS 请求；timeout 限制连接、读取等操作的等待时间。
        # 整个工作流步骤的运行时间上限在 YAML 中设置。
        with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            if response.headers.get('Content-Encoding', '').lower() not in ('', 'identity'):
                fail('model_invalid_response', http_status=response.status)
            if int(response.headers.get('Content-Length') or 0) > max_bytes:
                fail('model_invalid_response', http_status=response.status)

            # 即使服务端未声明长度，也最多读取上限再加一个字节。
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes: fail('model_invalid_response', http_status=response.status)
            return Response(response.status, content)
    except HTTPError as error:
        # HTTP 错误只保留状态码，不读取可能包含敏感信息的错误正文。
        error.close()
        return Response(error.code)
    except URLError as error:
        fail('model_timeout' if isinstance(error.reason, TimeoutError) else 'model_unavailable')


def keys_only(value, allowed): return obj(value) and value.keys() <= set(allowed)


def detect_runtime(env):
    # 根据环境变量识别本地运行或 CI 运行，后面会检查它是否与配置一致。
    if any(env.get(k) and str(env[k]).lower() not in ('false', '0') for k in ('GITHUB_RUN_ID', 'GITLAB_CI', 'BUILD_BUILDID', 'JENKINS_URL', 'CIRCLECI', 'BUILDKITE', 'TEAMCITY_VERSION')): return 'ci'

    ci = False
    for key in ('CI', 'GITHUB_ACTIONS'):
        value = str(env.get(key, '')).lower()
        if value in ('true', '1'): ci = True
        elif value not in ('', 'false', '0'): return 'unknown'

    return 'ci' if ci else 'local'


def resolve_model_config(config, env=None):
    # 检查运行环境，以及配置是否允许把公开或测试材料发给模型，再检查接口参数。
    # 此处只检查密钥的环境变量名，不读取密钥。
    env = os.environ if env is None else env
    if not keys_only(config, ['mode', 'runtime', 'allowExternalProcessing', 'material', 'timeoutMs', 'api']) or config.get('mode') != 'api': fail('model_invalid_config')

    runtime = detect_runtime(env)
    if config.get('runtime') not in ('local', 'ci') or config['runtime'] != runtime: fail('model_runtime_mismatch')
    if config.get('allowExternalProcessing') is not True or config.get('material') not in ('synthetic', 'public'): fail('model_processing_not_authorized')

    timeout = config.get('timeoutMs') if config.get('timeoutMs') is not None else 20_000
    if not integer(timeout, 1, 120_000): fail('model_invalid_config')

    api = config.get('api')
    if not keys_only(api, ['endpoint', 'model', 'keyEnv', 'maxOutputTokens', 'thinking']) or not isinstance(api.get('model'), str) or not rx(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}').fullmatch(api['model']) or not isinstance(api.get('keyEnv'), str) or not rx(r'VALUE_MODEL_[A-Z0-9_]+').fullmatch(api['keyEnv']) or not integer(output_tokens(api), 128, 8192) or not valid_https(api.get('endpoint')): fail('model_invalid_config')
    if 'thinking' in api and api['thinking'] not in ('enabled', 'disabled'): fail('model_invalid_config')

    return dict(backend='api', runtime=runtime, timeoutMs=timeout, api=api)


def read_model_config(path):
    # 配置文件读不出、JSON 解析失败或内容为 null 时，返回 invalidConfig，让后续代码报告错误。
    try:
        value = read_json_file(path, 32_000)
        return value if value is not None else {'invalidConfig': True}
    except Exception: return {'invalidConfig': True}


def output_tokens(config):
    return config.get('maxOutputTokens') if config.get('maxOutputTokens') is not None else 4096


def call_api(messages, config, env, timeout, fetch):
    # 使用非流式 JSON 回复，一次收齐内容后再校验每项改动的判断。
    payload = dict(model=config['model'], messages=messages, stream=False, max_tokens=output_tokens(config), response_format={'type': 'json_object'})
    if 'thinking' in config: payload['thinking'] = {'type': config['thinking']}
    if config.get('thinking') == 'disabled': payload['temperature'] = 0
    body = dumps(payload).encode('utf-8')
    if len(body) > MAX_MODEL_REQUEST_BYTES: fail('model_scope_limit')

    # 配置中仅保存环境变量名；真实密钥只用于本次请求头。
    key = env.get(config['keyEnv'])
    if not isinstance(key, str) or not key.strip() or '\r' in key or '\n' in key: fail('model_missing_api_key')

    response = fetch(config['endpoint'], method='POST', headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, body=body, timeout=timeout, max_bytes=64_000)
    if not response.ok: fail('model_api_error', http_status=response.status)

    def invalid(reason='invalid_schema', finish=None):
        fail('model_invalid_response', reason=reason, http_status=response.status, finish_reason=finish)

    try:
        if len(response.body) > 64_000: invalid()
        if not response.body.strip(): invalid('empty_response')
        envelope = loads(response.body.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError): invalid('invalid_json')
    if obj(envelope) and envelope.get('error') is not None: fail('model_api_error', http_status=response.status)
    if not obj(envelope) or not isinstance(envelope.get('choices'), list) or len(envelope['choices']) != 1: invalid()

    # 回复未生成完、模型拒答或要求调用工具时都报错；完整回复才交给 assessment.py 检查。
    choice = envelope['choices'][0]
    if not obj(choice): invalid()
    # 仅记录官方状态枚举，不记录服务端可能回显的任意文本。
    finish = choice.get('finish_reason')
    finish = finish if finish in ('stop', 'length', 'content_filter', 'tool_calls', 'insufficient_system_resource', 'function_call') else 'unknown'
    if finish == 'length': invalid('truncated_response', finish)
    if finish != 'stop' or not obj(choice.get('message')): invalid(finish=finish)
    message = choice['message']
    if message.get('role') != 'assistant' or (message.get('refusal') is not None and message['refusal'] != '') or (message.get('tool_calls') is not None and (not isinstance(message['tool_calls'], list) or len(message['tool_calls']) != 0)) or message.get('function_call') is not None: invalid(finish=finish)
    if message.get('content') is None or (isinstance(message['content'], str) and not message['content'].strip()): invalid('empty_response', finish)
    if not isinstance(message['content'], str): invalid(finish=finish)

    return ModelText(message['content'], response.status, finish)


def invoke_model(messages, selected, *, env=None, fetch=request):
    # fetch 可在测试中替换。此层只请求一次，重试由包含契约校验的上层统一控制。
    env = os.environ if env is None else env
    started = time.monotonic()

    try:
        timeout = selected['timeoutMs'] / 1000
        result = call_api(messages, selected['api'], env, timeout, fetch)

        # 这是完成后的耗时检查，并不会主动中断尚未返回的网络操作。
        if time.monotonic() - started > timeout:
            fail('model_timeout', http_status=result.http_status, finish_reason=result.finish_reason)
        return result
    except TimeoutError: fail('model_timeout')
    except ModelError: raise
    except Exception: fail('model_unavailable')
