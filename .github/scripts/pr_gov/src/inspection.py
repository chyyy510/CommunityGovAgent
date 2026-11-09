"""校验文档输入并提取改动；是否构成修复及修复是否正确交给模型。"""

from pathlib import PurePosixPath

from .common import obj, integer, valid_path, byte_len, controls, rx, I, get, loads
from .patch import parse_patch, patch_matches_head
from .resources import read_text

# 限制文件数量和内容大小，避免读取或发送给模型的内容过多。
MAX_FILES = 20
MAX_CONTENT_BYTES = 200_000


def title_matches(title):
    # 与 GitHub 读取步骤共用规则，本地测试也先筛标题。
    if not isinstance(title, str) or not title.strip(): return False

    rules = loads(read_text('src/title_rules.json'))

    def matches(name): return bool(rx(rules[name], I).search(title))

    return matches('languageRepair') or (matches('documentation') and matches('repair'))


def documentation_path(path):
    # Markdown 和 rst 按扩展名识别；txt 仅接受常见说明文件或文档目录中的文件。
    p = PurePosixPath(path.lower())
    return p.suffix in ('.md', '.markdown', '.rst') or (
        p.suffix == '.txt' and (p.stem in ('readme', 'contributing') or p.parts[0] in ('doc', 'docs', 'documentation')))


def inspect_input(data, *, require_content=True):
    """检查文件和补丁，提取交给模型的改动；有问题时由调用方跳过整个 PR。

    require_content=False 时允许缺少文件正文，但缺少正文的文件不会提取改动。
    """
    result = dict(reasons=[], changes=[], documents={}, references=[], counts=dict(files=0, changedLines=0))

    def problem(code, path=None):
        reason = dict(code=code, **({'path': path} if path else {}))
        if reason not in result['reasons']: result['reasons'].append(reason)

    if not obj(data) or not integer(data.get('schemaVersion'), 1, 1) or not isinstance(data.get('files'), list):
        problem('invalid_input'); return result
    if data.get('complete') is not True: problem('incomplete_file_list')

    files = data['files']
    result['counts']['files'] = len(files)
    if not files: problem('no_changes')
    if len(files) > MAX_FILES: problem('scope_limit'); return result

    seen = set()
    for file in files:
        if not obj(file) or not valid_path(file.get('filename')):
            problem('invalid_file'); continue
        path = file['filename']
        if path in seen: problem('duplicate_file', path); continue
        seen.add(path)

        if not documentation_path(path): problem('outside_documentation_scope', path)
        if file.get('status') != 'modified': problem('unsupported_file_status', path); continue
        if not all(integer(file.get(key)) for key in ('additions', 'deletions')):
            problem('missing_change_counts', path); continue
        result['counts']['changedLines'] += file['additions'] + file['deletions']

        patch = file.get('patch')
        if not isinstance(patch, str) or not patch or byte_len(patch) > MAX_CONTENT_BYTES:
            problem('missing_or_oversized_patch', path); continue
        try: parsed = parse_patch(patch)
        except ValueError: problem('invalid_or_truncated_patch', path); continue

        # 用接口给出的增删行数核对补丁，避免把截断的 diff 当作完整改动。
        if parsed['additions'] != file['additions'] or parsed['deletions'] != file['deletions']:
            problem('patch_count_mismatch', path)

        content = file.get('headContent')
        if not isinstance(content, str):
            if require_content: problem('missing_document_context', path)
            continue
        if byte_len(content) > MAX_CONTENT_BYTES or controls(content):
            problem('invalid_document_content', path); continue
        if not patch_matches_head(content, parsed): problem('patch_context_mismatch', path); continue

        # 从已核对的 head 正文逆向应用完整 diff，base 与 head 各自保留真实行号。
        lines = content.split('\n')
        if lines[-1] == '': lines.pop()
        base = list(lines)
        for block in reversed(parsed['blocks']):
            start = block['newStart'] - 1
            base[start:start + len(block['added'])] = [line['text'] for line in block['removed']]
        result['documents'][path] = dict(base=base, head=lines)

        # 连续增删块仍形成原有 change；上下文在构造每批请求时按文件共享。
        for block in parsed['blocks']:
            before = '\n'.join(line['text'] for line in block['removed'])
            after = '\n'.join(line['text'] for line in block['added'])
            if controls(before) or controls(after): problem('invalid_document_content', path); continue

            first = block['newStart']
            result['changes'].append(dict(id=f"change-{len(result['changes']) + 1}", path=path,
                oldLine=block['oldStart'], newLine=first, before=before, after=after,
                oldCount=len(block['removed']), newCount=len(block['added'])))

    # 参考材料由调用方提供；当前 GitHub 读取步骤不会自动收集代码参考。
    refs = data.get('references', [])
    if not isinstance(refs, list) or len(refs) > 8:
        problem('invalid_references'); return result

    ids, total = set(), 0
    for ref in refs:
        source = get(ref, 'source')
        if (not obj(ref) or not isinstance(ref.get('id'), str) or not rx(r'[A-Za-z0-9_-]{1,64}').fullmatch(ref['id'])
            or ref['id'] in ids or ref.get('kind') not in ('code', 'configuration')
            or not isinstance(ref.get('content'), str) or not ref['content'].strip() or controls(ref['content'])
            or not obj(source) or source.get('trust') != 'adapter_verified'
            or source.get('provider') not in ('github', 'local', 'atomgit')
            or not valid_path(source.get('path')) or not integer(source.get('startLine'), 1)
            or not isinstance(source.get('revision'), str) or not rx(r'[a-f0-9]{40}(?:[a-f0-9]{24})?', I).fullmatch(source['revision'])):
            problem('invalid_references'); continue

        # 参考材料必须来自本次检查的 PR 提交（headSha），不能混用其他提交中的内容。
        head = get(data, 'source', 'headSha')
        if not isinstance(head, str) or source['revision'].lower() != head.lower():
            problem('reference_revision_mismatch'); continue

        ids.add(ref['id'])
        total += byte_len(ref['content'])
        if byte_len(ref['content']) > 4000 or total > 12_000: problem('reference_scope_limit')
        result['references'].append(dict(id=ref['id'], kind=ref['kind'], content=ref['content'], source=source))

    return result
