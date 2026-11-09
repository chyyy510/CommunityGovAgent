"""根据判断结果和模板生成评论正文；发布由工作流负责。"""

from collections import Counter

from .common import get
from .resources import escape, read_messages, render_template, substitute

# publish_comment.cjs 用这个标记查找以前发过的评论；修改时两处要保持一致。
MARKER = '<!-- truecontribution:documentation-repair-review:v1 -->'


def review_summary(result, messages):
    """复用分类目的和逐项结果，只汇总展示，不重新判断修复是否正确。"""
    text = messages['comment']
    paragraphs = []
    if get(result, 'classification', 'reason'):
        paragraphs.append(substitute(text['classification'], reason=escape(result['classification']['reason'])))

    evidence = result.get('evidence', [])
    if get(result, 'modelReview', 'status') != 'completed':
        paragraphs.append(text['not_started'] if result.get('screening') != 'eligible' else text['unavailable'])
    elif not evidence:
        paragraphs.append(text['no_evidence'])
    else:
        types = {item['type'] for item in evidence}
        scope = '、'.join(label for kind, label in (
            ('language', '语言表达调整'), ('factual', '技术事实修改')) if kind in types)
        paragraphs.append(substitute(text['review_scope'], scope=scope))
        counts = Counter(item['assessment'] for item in evidence)
        abnormal = sum(counts[kind] for kind in ('unchanged', 'harmful', 'uncertain'))
        if not abnormal:
            paragraphs.append(substitute(text['all_improved'], count=len(evidence)))
            # 只提取一条最常见的审阅要点，并标明覆盖数量，不扩展成所有修改的事实。
            reason, count = Counter(item['reason'] for item in evidence).most_common(1)[0]
            paragraphs.append(substitute(text['repair_summary'], count=count, reason=escape(reason)))
        else:
            paragraphs.append(substitute(text['has_issues'], total=len(evidence),
                improved=counts['improved'], count=abnormal))

    # 每类异常保留最常见的完整理由，避免截断否定或限定条件；其余理由留在折叠区。
    for kind in ('harmful', 'unchanged', 'uncertain'):
        reasons = Counter(item['reason'] for item in evidence if item['assessment'] == kind)
        if reasons:
            reason, _ = reasons.most_common(1)[0]
            paragraphs.append(substitute(text['issue_summary'], assessment=messages['assessments'][kind],
                count=sum(reasons.values()), reason=escape(reason),
                more=text['more_issues'] if len(reasons) > 1 else ''))

    for reason in result.get('reasons', []):
        code = reason['code']
        # 已有逐项结果的总结覆盖这两条通用说明，避免重复。
        if evidence and get(result, 'modelReview', 'status') == 'completed' and code in (
            'model_supported_repair', 'model_requires_review'):
            continue
        paragraphs.append(substitute(text['reason'],
            message=escape(messages['reasons'].get(code, text['unknown_reason'])), code=escape(code)))
    return '\n\n'.join(paragraphs)


def render_comment(result):
    messages = read_messages()
    text = messages['comment']

    # 必须有已完成的模型判断，才能展示“建议合并”。
    supported = (result.get('status') == 'candidate'
        and result.get('recommendation') == 'lightweight_review'
        and get(result, 'modelReview', 'status') == 'completed')

    # 完整逐项理由只在详细依据中展开，总结独立展示异常数量和主要问题。
    lines = []
    for item in result.get('evidence', []):
        lines.append(substitute(text['evidence'], path=escape(item['path'][:500]), line=item['newLine'],
            assessment=messages['assessments'][item['assessment']], reason=escape(item['reason'])))

    # 明确区分“判断后不建议合并”和“模型未能完成判断”。
    skipped = result.get('screening') != 'eligible'
    details = (substitute(text['details'], count=len(lines), evidence='\n'.join(lines))
        if lines else text['no_evidence'])
    changes = (len(lines) if lines and get(result, 'modelReview', 'status') == 'completed'
        else text['unknown_count'])
    counts = substitute(text['counts'], files=get(result, 'counts', 'files') or 0,
        changes=changes, lines=get(result, 'counts', 'changedLines') or 0)

    # 转义 @，避免文件名或模型解释中的 @用户名 触发通知；正文最多 60000 字符。
    body = render_template('pr_comment.md', marker=MARKER,
        recommendation=text['not_reviewed' if skipped else 'supported' if supported else 'unsupported'],
        head_sha=escape(get(result, 'source', 'headSha') or text['unknown_revision']),
        base_sha=escape(get(result, 'source', 'baseSha') or text['unknown_revision']),
        counts=counts, summary=review_summary(result, messages), details=details).replace('@', '&#64;')
    if len(body) > 60_000: raise ValueError('comment_size_limit')
    return body
