"""共享上下文、版本行号、请求体积及分批完整性回归；不调用真实模型。"""

from difflib import unified_diff
import json
import unittest
from unittest.mock import Mock

from test_assessment import CONFIG, reply, review_changes, sample
from src.assessment import MAX_MESSAGE_BYTES, assess, request_messages, retry_messages, review_batches
from src.common import byte_len, dumps
from src.contexts import shared_files
from src.inspection import inspect_input
from src.model import ModelError


def document(before, after, path='docs/guide.md'):
    patch = ''.join(unified_diff([s + '\n' for s in before], [s + '\n' for s in after], n=0)).split('\n', 2)[2]
    return dict(filename=path, status='modified', patch=patch,
                additions=sum(line.startswith('+') for line in patch.splitlines()),
                deletions=sum(line.startswith('-') for line in patch.splitlines()),
                headContent='\n'.join(after) + ('\n' if after else ''))


def input_files(*files):
    return {**sample(), 'files': list(files)}


def context_lines(file, version):
    return [(context['startLine'] + offset, text) for context in file['contexts']
            if context['version'] == version for offset, text in enumerate(context['text'].split('\n'))]


class ContextTests(unittest.TestCase):
    def assert_original_lines(self, inspected, files):
        for file in files:
            ids = {context['id']: context for context in file['contexts']}
            self.assertEqual(len(ids), len(file['contexts']))
            used = set()
            for version, line_key, count_key, text_key in (
                    ('base', 'oldLine', 'oldCount', 'before'), ('head', 'newLine', 'newCount', 'after')):
                actual = context_lines(file, version)
                for context in file['contexts']:
                    if context['version'] == version:
                        self.assertEqual(len(context['text'].split('\n')), context['endLine'] - context['startLine'] + 1)
                for change in file['changes']:
                    self.assertEqual(change['path'], file['path'])
                    if change[count_key]:
                        actual.extend((change[line_key] + offset, text) for offset, text in enumerate(change[text_key].split('\n')))
                    for context_id in change['contextIds'][version]:
                        self.assertEqual(ids[context_id]['version'], version)
                        used.add(context_id)
                numbers = [number for number, _ in actual]
                self.assertEqual(len(numbers), len(set(numbers)), '同一文件、版本、行号只能携带一次原文')
                for number, text in actual:
                    self.assertEqual(text, inspected['documents'][file['path']][version][number - 1])
            self.assertEqual(used, ids.keys())

    def test_相邻修改合并重叠窗口且保留完整改动(self):
        base = [f'原始行 {i}' for i in range(20)]
        head = list(base)
        head[5], head[8] = '修改一', '修改二'
        inspected = inspect_input(input_files(document(base, head)))
        files = shared_files(inspected)
        self.assert_original_lines(inspected, files)
        changes = files[0]['changes']
        for version in ('base', 'head'):
            self.assertTrue(set(changes[0]['contextIds'][version]) & set(changes[1]['contextIds'][version]))
            self.assertEqual([number for number, _ in context_lines(files[0], version)],
                             [number for number in range(1, 15) if number not in (6, 9)])
        for change, original in zip(changes, inspected['changes']):
            self.assertEqual({key: change[key] for key in original}, original)

    def test_相接但不重叠的区间合并(self):
        base = [f'行 {i}' for i in range(20)]
        head = list(base)
        head[1], head[12] = '一', '二'
        inspected = inspect_input(input_files(document(base, head)))
        files = shared_files(inspected)
        self.assert_original_lines(inspected, files)
        for version in ('base', 'head'):
            contexts = [c for c in files[0]['contexts'] if c['version'] == version]
            self.assertIn((3, 12), [(c['startLine'], c['endLine']) for c in contexts])

    def test_跨文件及不同位置相同文本不按内容去重(self):
        base = ['重复正文'] * 40
        head = list(base)
        head[5], head[30] = '修改一', '修改二'
        first = document(base, head)
        # 固定修改位置，避免 diff 算法对完全重复的行选择等效移动。
        first.update(patch='@@ -6 +6 @@\n-重复正文\n+修改一\n@@ -31 +31 @@\n-重复正文\n+修改二\n', additions=2, deletions=2)
        inspected = inspect_input(input_files(first, {**first, 'filename': 'docs/second.md'}))
        files = shared_files(inspected)
        self.assertEqual([f['path'] for f in files], ['docs/guide.md', 'docs/second.md'])
        self.assert_original_lines(inspected, files)
        for file in files:
            for version in ('base', 'head'):
                self.assertEqual([text for _, text in context_lines(file, version)], ['重复正文'] * 20)

    def test_插入删除造成偏移时两侧行号及空行均对应原文(self):
        base = [f'原始行 {i}' for i in range(30)]
        head = ['开头新增', *base[:4], '', '新增说明', *base[4:15], *base[17:24], '替换说明', *base[25:]]
        inspected = inspect_input(input_files(document(base, head)))
        self.assertEqual(inspected['reasons'], [])
        self.assertEqual(inspected['documents']['docs/guide.md'], dict(base=base, head=head))
        self.assertTrue(any(c['oldLine'] != c['newLine'] for c in inspected['changes']))
        self.assertTrue(any(c['oldCount'] == 0 for c in inspected['changes']))
        self.assertTrue(any(c['newCount'] == 0 for c in inspected['changes']))
        self.assert_original_lines(inspected, shared_files(inspected))

    def test_空白行插入与空文件边界保留行数(self):
        for base, head in ((['首行'], ['首行', '']), ([], ['']), ([''], [])):
            with self.subTest(base=base, head=head):
                inspected = inspect_input(input_files(document(base, head)))
                self.assertEqual(inspected['documents']['docs/guide.md'], dict(base=base, head=head))
                self.assert_original_lines(inspected, shared_files(inspected))
                change = inspected['changes'][0]
                self.assertEqual(change['oldCount'] + change['newCount'], 1)
                self.assertEqual((change['before'], change['after']), ('', ''))

    def test_标题层级修改补充远处相邻及上级标题而不携带长正文(self):
        base = ['# 根标题', *(['很长的无关正文' * 100] * 8), '## 上级标题', '### 前一个标题',
                *(['无关正文'] * 8), '#### 当前标题', *(['无关正文'] * 9), '### 后一个标题']
        head = list(base)
        head[19] = '### 当前标题'
        inspected = inspect_input(input_files(document(base, head)))
        files = shared_files(inspected)
        self.assert_original_lines(inspected, files)
        for version in ('base', 'head'):
            self.assertEqual([text for _, text in context_lines(files[0], version)],
                             ['# 根标题', '## 上级标题', '### 前一个标题', '### 后一个标题'])

    def test_Setext标题保留文字且代码围栏内伪标题不作为祖先(self):
        base = ['# 根标题', '```md', '# 伪标题', '```', *(['正文'] * 8), '当前标题', '---',
                *(['正文'] * 8), '## 后一个标题']
        head = list(base)
        head[13] = '==='
        inspected = inspect_input(input_files(document(base, head)))
        files = shared_files(inspected)
        self.assert_original_lines(inspected, files)
        for version in ('base', 'head'):
            self.assertEqual([text for _, text in context_lines(files[0], version)], ['# 根标题', '当前标题', '## 后一个标题'])

    def test_标题文字同时修改仍保留周边正文(self):
        base = ['# 根标题', '必要前文', '### 错误标题', '必要后文']
        head = ['# 根标题', '必要前文', '## 正确标题', '必要后文']
        files = shared_files(inspect_input(input_files(document(base, head))))
        for version in ('base', 'head'):
            self.assertEqual([text for _, text in context_lines(files[0], version)], ['# 根标题', '必要前文', '必要后文'])

    def test_总输入超限按真实字节拆批并合并乱序回复(self):
        files = [document(['旧文' * 1800], ['新文' * 1800], f'docs/{i}.md') for i in range(4)]
        data = input_files(*files)
        inspected = inspect_input(data)
        with self.assertRaisesRegex(ModelError, 'model_scope_limit'): request_messages(inspected, data['title'])
        invoke = Mock(wraps=reply(mutate=lambda response: response['assessments'].reverse()))
        result = assess(data, config=CONFIG, env={}, invoke=invoke)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(invoke.call_count, 5)
        submitted = []
        for call in invoke.call_args_list[1:]:
            messages = call.args[0]
            self.assertLessEqual(byte_len(dumps(messages)), MAX_MESSAGE_BYTES)
            payload = json.loads(messages[1]['content'])
            self.assert_original_lines(inspected, payload['files'])
            submitted.extend(review_changes(payload))
        self.assertEqual([c['id'] for c in submitted], [c['id'] for c in inspected['changes']])
        self.assertEqual([(c['id'], c['before'], c['after']) for c in result['evidence']],
                         [(c['id'], c['before'], c['after']) for c in inspected['changes']])

    def test_同文件分批只携带当前批次上下文且每项恰好评估一次(self):
        base = [f'旧文 {i}' if i % 2 == 0 else '上下文' for i in range(24)]
        head = [f'新文 {i}' if i % 2 == 0 else '上下文' for i in range(24)]
        data = input_files(document(base, head))
        inspected = inspect_input(data)
        batches = list(review_batches(inspected, data['title']))
        self.assertEqual([len(batch['changes']) for batch, _ in batches], [5, 5, 2])
        for batch, messages in batches:
            self.assert_original_lines(batch, json.loads(messages[1]['content'])['files'])
        result = assess(data, config=CONFIG, env={}, invoke=reply(mutate=lambda response: response['assessments'].reverse()))
        self.assertEqual([e['id'] for e in result['evidence']], [c['id'] for c in inspected['changes']])
        self.assertEqual(result['status'], 'candidate')

    def test_单项无法容纳仍审阅其他项且不发布部分结果(self):
        data = input_files(document(['小旧文'], ['小新文'], 'docs/first.md'),
                           document(['a' * 24000], ['b' * 24000], 'docs/huge.md'),
                           document(['小旧文'], ['小新文'], 'docs/last.md'))
        invoke = Mock(wraps=reply())
        result = assess(data, config=CONFIG, env={}, invoke=invoke)
        submitted = [c['id'] for call in invoke.call_args_list[1:] for c in review_changes(json.loads(call.args[0][1]['content']))]
        self.assertEqual(submitted, ['change-1', 'change-3'])
        self.assertEqual(result['reasons'], [dict(code='model_scope_limit')])
        self.assertEqual(result['modelReview']['status'], 'unavailable')
        self.assertEqual(result['evidence'], [])

    def test_消息上限包含序列化转义及重试提醒(self):
        data = sample('旧文 " \\ ' * 100, '新文 " \\ ' * 100)
        inspected = inspect_input(data)
        messages = request_messages(inspected, data['title'])
        padding = MAX_MESSAGE_BYTES - byte_len(dumps(retry_messages(messages, 'review', True)))
        data['title'] += 'x' * padding
        batches = list(review_batches(inspected, data['title']))
        self.assertEqual(len(batches), 1)
        self.assertEqual(byte_len(dumps(retry_messages(batches[0][1], 'review', True))), MAX_MESSAGE_BYTES)
        invoke = Mock(side_effect=[dumps(dict(schemaVersion=1, type='documentation_repair', reason='修复文档。')),
                                   ModelError('model_invalid_response', reason='truncated_response'),
                                   reply()(batches[0][1], None)])
        with self.assertLogs('src.assessment', level='WARNING'):
            result = assess(data, config=CONFIG, env={}, invoke=invoke)
        self.assertEqual(result['status'], 'candidate')
        self.assertTrue(all(byte_len(dumps(call.args[0])) <= MAX_MESSAGE_BYTES for call in invoke.call_args_list))
        exact_title = data['title'] + 'x' * (MAX_MESSAGE_BYTES - byte_len(dumps(batches[0][1])))
        self.assertEqual(byte_len(dumps(request_messages(inspected, exact_title))), MAX_MESSAGE_BYTES)
        with self.assertRaisesRegex(ModelError, 'model_scope_limit'): request_messages(inspected, exact_title + 'x')

    def test_相邻长正文共享后请求体积下降(self):
        base = [f'行 {i} ' + '重复的长正文。' * 40 for i in range(20)]
        head = list(base)
        for index in (5, 7, 9, 11, 13):
            base[index], head[index] = f'旧文 {index}', f'新文 {index}'
        data = input_files(document(base, head))
        inspected = inspect_input(data)
        messages = request_messages(inspected, data['title'])
        legacy = []
        for change in inspected['changes']:
            first, last = change['newLine'], change['newLine'] + change['newCount'] - 1
            legacy.append({**{k: change[k] for k in ('id', 'path', 'oldLine', 'newLine', 'before', 'after')},
                           'previous': '\n'.join(head[max(0, first - 6):first - 1]),
                           'next': '\n'.join(head[last:last + 5])})
        # 同一 Prompt 下比较输入结构，避免把提示词长度差异误计为去重收益。
        old = [messages[0], dict(role='user', content=dumps(dict(title=data['title'], changes=legacy, references=[])))]
        self.assertLess(byte_len(dumps(messages)), byte_len(dumps(old)) * 0.9)


if __name__ == '__main__': unittest.main()
