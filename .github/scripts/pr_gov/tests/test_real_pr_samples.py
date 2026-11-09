"""真实 PR 固定快照回归；测试不联网、不读取真实密钥、不调用真实模型。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_assessment import CONFIG, reply, review_changes
from pr_checker import run_check
from src.assessment import assess
from src.common import byte_len, dumps
from src.inspection import inspect_input

FIXTURES = Path(__file__).resolve().parent / 'real_pr/cann-learning-hub'


def load_script(name):
    spec = importlib.util.spec_from_file_location('real_pr_' + name, FIXTURES / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealPRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads((FIXTURES / 'cases.json').read_text(encoding='utf-8'))
        cls.sources = json.loads((FIXTURES / 'sources.json').read_text(encoding='utf-8'))
        cls.inputs = {case['id']: case['input'] for case in load_script('run').load_cases()['samples']}

    def test_七个真实PR的完整文件列表与固定快照哈希(self):
        self.assertEqual(len(self.cases['samples']), 7)
        self.assertEqual(hashlib.sha256((FIXTURES / 'cases.json').read_bytes()).hexdigest(), self.sources['casesSha256'])
        self.assertEqual(hashlib.sha256((FIXTURES / 'UPSTREAM-LICENSE.txt').read_bytes()).hexdigest(), self.sources['license']['sha256'])
        sources = {row['number']: row for row in self.sources['samples']}
        collector = load_script('collect')
        for case in self.cases['samples']:
            data = case['input']
            number = int(case['id'].rsplit('-', 1)[1])
            source = sources[number]
            self.assertTrue(data['complete'])
            self.assertEqual(len(data['files']), source['fileCount'])
            self.assertEqual(hashlib.sha256(dumps(data).encode()).hexdigest(), source['inputSha256'])
            self.assertEqual(data['source']['headSha'], source['headSha'])
            self.assertEqual(data['source']['baseSha'], source['comparisonBaseSha'])
            self.assertEqual(source['baseVerification'], 'complete_compare_matches_pr_diff')
            self.assertTrue(source['snapshotStable'])
            for file, proof in zip(data['files'], source['files']):
                self.assertEqual(file['filename'], proof['filename'])
                self.assertEqual(hashlib.sha256(file['patch'].encode()).hexdigest(), proof['patchSha256'])
                if file['status'] != 'modified':
                    self.assertEqual(proof['base']['blobSha'], proof['head']['blobSha'])
                    self.assertTrue(proof['renameBlobUnchanged'])
                    continue
                for field, version in [('baseContent', 'base'), ('headContent', 'head')]:
                    raw = file[field].encode('utf-8')
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), proof[version]['sha256'])
                    self.assertEqual(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(), proof[version]['blobSha'])
                collector.verify_patch(file['baseContent'], file['headContent'], file['patch'])

    def test_真实PR先筛标题再检查文件范围(self):
        eligible = {1407, 1242, 1405, 208, 1380}
        expected_reasons = {528: 'outside_documentation_scope', 1135: 'title_not_matched'}
        for case in self.cases['samples']:
            number = int(case['id'].rsplit('-', 1)[1])
            invoke = Mock(side_effect=AssertionError('范围外不应调用模型'))
            result = assess(self.inputs[case['id']], config=None if number in eligible else CONFIG, env={}, invoke=invoke)
            self.assertEqual(result['screening'], 'skipped')
            self.assertEqual(result['status'], 'uncertain')
            codes = [reason['code'] for reason in result['reasons']]
            self.assertIn('model_not_configured' if number in eligible else expected_reasons[number], codes)
            invoke.assert_not_called()

    def test_真实输入进入工作流不写文件且离线输入不访问平台(self):
        for case in self.cases['samples']:
            number = int(case['id'].rsplit('-', 1)[1])
            with tempfile.TemporaryDirectory() as temp:
                path, output = Path(temp, 'input.json'), Path(temp, 'outputs')
                path.write_text(dumps(self.inputs[case['id']]), encoding='utf-8')
                result = run_check(env={'RUNNER_TEMP': temp, 'INPUT_FILES-JSON': str(path),
                    'GITHUB_OUTPUT': str(output)})
                self.assertEqual(list(Path(temp).iterdir()), [path])
                self.assertNotIn('comment', result)

    def test_本地环境文件只加载模型变量且不执行文本(self):
        runner = load_script('run')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp, '.env')
            path.write_text('export DEEPSEEK_API_KEY="synthetic-key"\nLLM_MODEL=deepseek-flash\n'
                            'OTHER_SECRET=unused\nCOMMAND="$(echo ignored)"\n', encoding='utf-8')
            env = runner.model_environment(path)
            self.assertEqual(env['VALUE_MODEL_API_KEY'], 'synthetic-key')
            self.assertEqual(env['LLM_MODEL'], 'deepseek-flash')
            self.assertNotIn('OTHER_SECRET', env)
            self.assertNotIn('COMMAND', env)

    def test_本地离线测试为所有结果生成报告和评论预览(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'results'
            completed = subprocess.run([sys.executable, '-I', str(FIXTURES/'run.py'), '--output-dir', str(output)],
                capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((output/'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['summary']['samples'], 7)
            self.assertEqual(report['execution'], 'offline')
            self.assertEqual(report['modelRequests'], 0)
            self.assertEqual(report['platformWrites'], 0)
            for name in ('documentation_classification.md', 'documentation_repair.md'):
                path = FIXTURES.parents[2] / 'prompts' / name
                self.assertEqual(report['sourceSha256']['prompts/' + name], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(len(list(output.glob('*.md'))), 7)
            self.assertEqual(report['samples'][0]['report']['reasons'][0]['code'], 'model_not_configured')

    def test_较大真实PR分批后完整校验(self):
        case = next(c for c in self.cases['samples'] if c['id'].endswith('-1380'))
        invoke = Mock(wraps=reply())
        result = assess(self.inputs[case['id']], config=CONFIG, env={}, invoke=invoke)
        self.assertEqual(invoke.call_count, 8)
        self.assertEqual(result['screening'], 'eligible')
        self.assertEqual(result['modelReview']['status'], 'completed')
        self.assertEqual([e['id'] for e in result['evidence']], [f'change-{i}' for i in range(1, 35)])
        requests = [call.args[0] for call in invoke.call_args_list[1:]]
        sizes = [byte_len(dumps(messages)) for messages in requests]
        # 优化前 7 批实际消息共 76,925 字节；包含当时的 system Prompt。
        self.assertLess(sum(sizes), 76_925)
        self.assertTrue(all(size <= 45_000 for size in sizes))
        self.assertEqual([c['id'] for messages in requests for c in review_changes(json.loads(messages[1]['content']))],
                         [e['id'] for e in result['evidence']])

    def test_真实快照重建base与归档原文行号一致(self):
        for data in self.inputs.values():
            inspected = inspect_input(data)
            for file in data['files']:
                if file['filename'] not in inspected['documents']: continue
                lines = file['baseContent'].split('\n')
                if lines[-1] == '': lines.pop()
                self.assertEqual(inspected['documents'][file['filename']]['base'], lines)


if __name__ == '__main__':
    unittest.main()
