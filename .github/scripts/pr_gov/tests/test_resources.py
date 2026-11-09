"""提示词、模板的文件读取及既有评论格式回归。"""
from contextlib import chdir
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_assessment import sample
from src import resources
from src.assessment import classification_messages, request_messages, validate_classification, validate_response
from src.comments import render_comment
from src.inspection import inspect_input

FIXTURES = Path(__file__).resolve().parent / 'real_pr/cann-learning-hub'


def current_report(report):
    # 仅转换历史记录的展示字段以核对模板；不将旧模型回复当作新契约的实测结果。
    result = copy.deepcopy(report)
    for item in result.get('evidence', []):
        item['type'] = item.pop('kind')
        item['assessment'] = item.pop('verdict')
    return result


class ResourceTests(unittest.TestCase):
    def test_从其他目录启动不读取调用方伪造的提示词和模板(self):
        expected = request_messages(inspect_input(sample()))
        expected_classification = classification_messages(sample())
        report = json.loads((FIXTURES/'results/deepseek/report.json').read_text(encoding='utf-8'))
        result = current_report(next(row['report'] for row in report['samples'] if row['report']['screening'] == 'eligible'))
        comment = render_comment(result)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'prompts').mkdir()
            (root/'templates').mkdir()
            (root/'prompts/documentation_repair.md').write_text('untrusted prompt', encoding='utf-8')
            (root/'prompts/documentation_classification.md').write_text('untrusted prompt', encoding='utf-8')
            (root/'templates/pr_comment.md').write_text('untrusted template', encoding='utf-8')
            (root/'templates/messages.json').write_text('{}', encoding='utf-8')
            with chdir(root):
                self.assertEqual(request_messages(inspect_input(sample())), expected)
                self.assertEqual(classification_messages(sample()), expected_classification)
                self.assertEqual(render_comment(result), comment)

    def test_提示词直接读文件且缺失时不使用隐含默认值(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(resources, 'ROOT', Path(directory)):
            Path(directory, 'prompts').mkdir()
            for name, request in (
                ('documentation_classification.md', lambda: classification_messages(sample())),
                ('documentation_repair.md', lambda: request_messages(inspect_input(sample()))),
            ):
                path = Path(directory, 'prompts', name)
                for text in ('# First prompt\n\n- Rule one', '# Updated prompt\n\n- Rule two'):
                    path.write_text(text + '\n', encoding='utf-8')
                    self.assertEqual(request()[0]['content'], text)
                path.unlink()
                with self.assertRaises(FileNotFoundError): request()

    def test_Markdown提示词中的JSON示例符合解析要求(self):
        def example(name):
            text = resources.read_text('prompts/' + name)
            value, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
            return value

        classification = example('documentation_classification.md')
        self.assertEqual(validate_classification(json.dumps(classification))['type'], 'documentation_repair')

        review = example('documentation_repair.md')
        item = review['assessments'][0]
        self.assertEqual(set(item), {'id', 'type', 'assessment', 'reason', 'references'})
        inspection = dict(changes=[dict(id=item['id'], before='修改前', after='修改后')],
            references=[dict(id=r['id'], content=r['quote']) for r in item['references']])
        self.assertEqual(validate_response(json.dumps(review), inspection), [dict(item, before='修改前', after='修改后')])

    def test_八份历史结果按新层次展示且保留结论和完整依据(self):
        count = 0
        for folder in ('offline', 'deepseek'):
            path = FIXTURES/'results'/folder
            report = json.loads((path/'report.json').read_text(encoding='utf-8'))
            for row in report['samples']:
                if row['report']['screening'] != 'eligible': continue
                result = current_report(row['report'])
                body = render_comment(result)
                archived = (path/(row['id'] + '.md')).read_text(encoding='utf-8')
                recommendation = next(line for line in archived.splitlines() if line.startswith('**'))
                self.assertIn(recommendation, body)
                self.assertIn('### 审阅总结', body)
                if result['evidence']:
                    detail = body.split('<details>', 1)[1].split('</details>', 1)[0]
                    for item in result['evidence']:
                        self.assertIn(resources.escape(item['reason']).replace('@', '&#64;'), detail)
                else:
                    self.assertNotIn('<details>', body)
                count += 1
        self.assertEqual(count, 8)

    def test_数据中的占位符不被二次替换且模板缺字段时报错(self):
        self.assertEqual(resources.substitute('$value', value='$other ${name} {expression}'), '$other ${name} {expression}')
        with self.assertRaises(KeyError): resources.substitute('$missing', value='text')


if __name__ == '__main__': unittest.main()
