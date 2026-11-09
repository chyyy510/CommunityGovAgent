"""先筛标题，再调用 LLM 分类；确认是文档修复后，分批检查修复是否正确。"""

import logging

from .common import dumps, loads, keys, byte_len, controls, integer
from .model import ModelError, invoke_model, resolve_model_config, output_tokens
from .inspection import inspect_input, title_matches
from .resources import read_text
from .contexts import shared_files

MAX_BATCH_CHANGES = 5
MAX_MESSAGE_BYTES = 45_000
LOGGER = logging.getLogger(__name__)


def parse_response(text, max_bytes):
    if not isinstance(text, str): raise ModelError('model_invalid_response', reason='invalid_schema')
    if not text.strip(): raise ModelError('model_invalid_response', reason='empty_response')
    if byte_len(text) > max_bytes: raise ModelError('model_invalid_response', reason='invalid_schema')
    try: return loads(text)
    except (ValueError, RecursionError): raise ModelError('model_invalid_response', reason='invalid_json') from None


def retry_messages(messages, stage, truncated=False):
    # 不回传无效原文；只补充固定格式提醒，避免把错误响应变成新指令。
    reminder = '上次输出未通过校验。请重新返回完整 JSON，严格遵循字段和枚举规则。'
    if stage == 'review': reminder += '每个输入 change ID 恰好出现一次，引文必须来自输入参考材料。'
    if truncated:
        reminder += '上次输出被截断，请缩短 reason 和引文，仅保留必要依据，不输出 before、after 或额外说明。'
    return [{**messages[0], 'content': messages[0]['content'] + '\n\n' + reminder}, *messages[1:]]


def validated_request(messages, selected, validate, *, stage, batch=None, env=None, invoke=invoke_model, **dependencies):
    # 每个阶段或批次最多两次请求，解析和逐项校验也在重试范围内。
    for attempt in (1, 2):
        text = None
        try:
            if byte_len(dumps(messages)) > MAX_MESSAGE_BYTES: raise ModelError('model_scope_limit')
            text = invoke(messages, selected, env=env, **dependencies)
            return validate(text)
        except ModelError as error:
            if error.code not in ('model_invalid_response', 'model_api_error', 'model_timeout', 'model_unavailable'):
                raise
            LOGGER.warning('模型调用或校验失败：%s', dumps(dict(
                stage=stage, batch=batch, attempt=attempt, model=selected['api']['model'],
                reason=error.reason, code=error.code,
                http_status=error.http_status if error.http_status is not None else getattr(text, 'http_status', None),
                finish_reason=error.finish_reason or getattr(text, 'finish_reason', None))))
            if attempt == 2: raise
            if error.reason == 'truncated_response':
                selected = {**selected, 'api': {**selected['api'], 'maxOutputTokens': min(8192, output_tokens(selected['api']) * 2)}}
            messages = retry_messages(messages, stage, error.reason == 'truncated_response')


def classification_messages(data):
    # 类型判断只发送标题和描述，绝不加入文件列表、补丁或参考代码。
    body = data.get('body') or ''
    if not isinstance(body, str): raise ModelError('invalid_input')

    messages = [dict(role='system', content=read_text('prompts/documentation_classification.md').rstrip('\n')),
        dict(role='user', content=dumps(dict(title=data['title'], body=body)))]
    if byte_len(dumps(messages)) > MAX_MESSAGE_BYTES: raise ModelError('model_scope_limit')
    return messages


def request_messages(inspection, title=''):
    # 提示词放在 system 消息中，PR 改动和参考材料放在 user 消息中。
    payload = dict(title=title, files=shared_files(inspection), references=[dict(id=r['id'], kind=r['kind'], text=r['content'], path=r['source']['path']) for r in inspection['references']])
    messages = [dict(role='system', content=read_text('prompts/documentation_repair.md').rstrip('\n')), dict(role='user', content=dumps(payload))]
    if byte_len(dumps(messages)) > MAX_MESSAGE_BYTES: raise ModelError('model_scope_limit')
    return messages


def review_batches(inspection, title):
    # 保留每批最多五项的输出规模约束，按实际消息大小提前拆批，并为重试提醒留空间。
    pending, messages, oversized = [], None, False
    for change in inspection['changes']:
        while True:
            batch = {**inspection, 'changes': [*pending, change]}
            try:
                candidate = request_messages(batch, title)
                if byte_len(dumps(retry_messages(candidate, 'review', True))) > MAX_MESSAGE_BYTES:
                    raise ModelError('model_scope_limit')
            except ModelError as error:
                if error.code != 'model_scope_limit': raise
                if pending:
                    yield {**inspection, 'changes': pending}, messages
                    pending, messages = [], None
                    continue
                # 单项也超限时继续审阅其余可容纳的改动，最后统一保留未完成状态。
                oversized = True
                break
            pending, messages = batch['changes'], candidate
            if len(pending) == MAX_BATCH_CHANGES:
                yield batch, messages
                pending, messages = [], None
            break
    if pending: yield {**inspection, 'changes': pending}, messages
    if oversized: raise ModelError('model_scope_limit')


def validate_classification(text):
    # 分类回复只回答类型和理由，不包含逐项评估或合并建议。
    def invalid(): raise ModelError('model_invalid_response', reason='invalid_schema')

    value = parse_response(text, 4000)

    if (not keys(value, ['schemaVersion', 'type', 'reason']) or not integer(value['schemaVersion'], 1, 1)
        or value['type'] not in ('documentation_repair', 'other', 'uncertain')
        or not isinstance(value['reason'], str) or not value['reason'].strip()
        or len(value['reason']) > 500 or controls(value['reason'])): invalid()

    return dict(type=value['type'], reason=value['reason'])


def validate_response(text, inspection):
    # 校验模型是否逐项回答且格式符合约定，不在此重新判断技术事实。
    def invalid(reason='invalid_schema'): raise ModelError('model_invalid_response', reason=reason)

    value = parse_response(text, 32_000)
    if not keys(value, ['schemaVersion', 'assessments']) or not integer(value['schemaVersion'], 1, 1) or not isinstance(value['assessments'], list): invalid()

    references = {r['id']: r for r in inspection['references']}
    changes = {c['id']: c for c in inspection['changes']}
    found = {}
    if len(value['assessments']) != len(changes): invalid('missing_or_duplicate_change')

    # 按 ID 核对完整性，再按原始输入顺序汇总，不能遗漏、重复或增加。
    for item in value['assessments']:
        if not keys(item, ['id', 'type', 'assessment', 'reason', 'references']): invalid()
        if not isinstance(item['id'], str) or item['id'] not in changes or item['id'] in found: invalid('missing_or_duplicate_change')
        change = changes[item['id']]

        if item['type'] not in ('language', 'factual') or item['assessment'] not in ('improved', 'unchanged', 'harmful', 'uncertain'): invalid()
        if not isinstance(item['reason'], str) or not item['reason'].strip() or len(item['reason']) > 500 or controls(item['reason']): invalid()

        if not isinstance(item['references'], list): invalid()

        seen = set()
        for citation in item['references']:
            # 引用是可选的；一旦提供，就必须来自本次输入中的参考材料。
            if not keys(citation, ['id', 'quote']) or not isinstance(citation['id'], str) or citation['id'] not in references or citation['id'] in seen: invalid('invalid_reference')
            quote = citation['quote']
            if not isinstance(quote, str) or not quote.strip() or quote not in references[citation['id']]['content']: invalid('invalid_reference')
            seen.add(citation['id'])

        # 原文完全由程序补入；最终 evidence 的字段保持兼容。
        found[item['id']] = dict(item, before=change['before'], after=change['after'])

    if found.keys() != changes.keys(): invalid('missing_or_duplicate_change')
    return [found[c['id']] for c in inspection['changes']]


def uncertain(code):
    # screening 只表示是否进入修复审阅；无论是否进入，都会生成结果评论。
    return dict(schemaVersion=2, status='uncertain', category='undetermined',
        recommendation='standard_review', reasons=[dict(code=code)], evidence=[], counts=dict(files=0, changedLines=0),
        screening='skipped', modelReview=dict(status='skipped'), limitations=['human_review_required', 'model_judgment_not_proof'])


def assess(data, *, config=None, env=None, invoke=invoke_model, **dependencies):
    # 输入有问题时直接结束，避免只评估 PR 中能够成功读取的一部分。
    result = uncertain('invalid_input')
    if not isinstance(data, dict): return result
    if isinstance(data.get('source'), dict):
        source = data['source']
        from .common import rx, I
        if all(isinstance(source.get(k), str) and rx(r'[a-f0-9]{40}(?:[a-f0-9]{24})?', I).fullmatch(source[k]) for k in ('baseSha', 'headSha')):
            result['source'] = {k: source[k] for k in ('baseSha', 'headSha')}
            if source.get('provider') in ('github', 'local', 'atomgit'): result['source']['provider'] = source['provider']

    if not title_matches(data.get('title')):
        result['reasons'] = [dict(code='title_not_matched')]
        return result

    inspected = inspect_input(data)
    result['counts'] = inspected['counts']
    result['reasons'] = inspected['reasons']

    if inspected['reasons']: return result

    if config is None or config == {'mode': 'off'}:
        result['reasons'] = [dict(code='model_not_configured')]
        return result

    try:
        selected = resolve_model_config(config, env)

        # 第一轮仅根据标题和描述分类，不能把关键词命中当作已经确认是文档修复。
        classification = validated_request(classification_messages(data), selected, validate_classification,
            stage='classification', env=env, invoke=invoke, **dependencies)
        result['classification'] = classification
        if classification['type'] != 'documentation_repair':
            result['reasons'] = [dict(code='not_documentation_repair' if classification['type'] == 'other' else 'classification_uncertain')]
            return result

        result['screening'] = 'eligible'
        result['category'] = 'documentation_repair'

        # 分类通过后分批审阅；全部批次校验成功前不发布部分 evidence。
        review = dict(status='unavailable')
        result['modelReview'] = review
        assessments = []
        for number, (batch, messages) in enumerate(review_batches(inspected, data['title']), 1):
            assessments.extend(validated_request(messages, selected,
                lambda text: validate_response(text, batch), stage='review', batch=number,
                env=env, invoke=invoke, **dependencies))
        review['status'] = 'completed'

        refs = {r['id']: r for r in inspected['references']}
        for change, item in zip(inspected['changes'], assessments):
            evidence = {**{k: change[k] for k in ('id', 'path', 'oldLine', 'newLine')}, **item}
            evidence['references'] = [{**c, 'source': refs[c['id']]['source']} for c in item['references']]
            result['evidence'].append(evidence)

        # 模型认为每处改动都修复了问题才建议合并。
        candidate = all(a['assessment'] == 'improved' for a in assessments)
        if candidate:
            result.update(status='candidate', category='documentation_repair', recommendation='lightweight_review')
        result['reasons'] = [dict(code='model_supported_repair' if candidate else 'model_requires_review')]
    except ModelError as error:
        result['modelReview']['status'] = 'unavailable'
        result['reasons'] = [dict(code=error.code)]

    return result
