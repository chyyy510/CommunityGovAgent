"""工作流入口和 GitHub 取数集成测试；不访问外部服务。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
import yaml
from test_assessment import sample, CONFIG, reply
from pr_checker import run_check
from src.comments import render_comment
from src.common import dumps

ROOT = Path(__file__).resolve().parents[1]


class ActionTests(unittest.TestCase):
    def test_未配置模型返回转人工且不生成报告或摘要(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp, 'input.json'); data.write_text(dumps(sample()), encoding='utf-8')
            output, summary = Path(temp, 'output'), Path(temp, 'summary')
            result = run_check(env={'RUNNER_TEMP': temp, 'GITHUB_WORKSPACE': temp, 'INPUT_FILES-JSON': 'input.json', 'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)})
            self.assertEqual(result['status'], 'uncertain')
            self.assertFalse(output.exists())
            self.assertFalse(summary.exists())
            self.assertEqual(list(Path(temp).iterdir()), [data])

    def test_模型配置与评论转义(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp, 'input.json'); data.write_text(dumps(sample(path='docs/<script>.md')), encoding='utf-8')
            config = Path(temp, 'config.json'); config.write_text(dumps(CONFIG), encoding='utf-8')
            result = run_check(env={'RUNNER_TEMP': temp, 'INPUT_FILES-JSON': str(data), 'INPUT_MODEL-CONFIG': str(config)}, invoke=reply())
            self.assertEqual(result['status'], 'candidate')
            result['source'] = dict(baseSha='a'*40, headSha='b'*40)
            self.assertNotIn('<script>', render_comment(result))

    def test_工作流不依赖报告输出目录(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp, 'input.json'); data.write_text(dumps(sample()), encoding='utf-8')
            missing = Path(temp, 'missing')
            result = run_check(env={'INPUT_FILES-JSON': str(data), 'RUNNER_TEMP': str(missing),
                'GITHUB_OUTPUT': str(missing/'output'), 'GITHUB_STEP_SUMMARY': str(missing/'summary')})
            self.assertEqual(result['status'], 'uncertain')
            self.assertFalse(missing.exists())

    def test_本地输入缺失损坏或超限时不调用模型和平台(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, 'input.json')
            for content, reason in [(None, 'input_unavailable'), ('{', 'invalid_input_json'),
                                    (' ' * 1_048_577, 'input_size_limit')]:
                if content is not None: path.write_text(content, encoding='utf-8')
                invoke = Mock(side_effect=AssertionError('不应调用模型'))
                result = run_check(env={'INPUT_FILES-JSON': str(path)}, invoke=invoke)
                self.assertEqual(result['reasons'][0]['code'], reason)
                self.assertEqual(result['screening'], 'skipped')
                invoke.assert_not_called()


    def test_执行真实YAML命令且隔离调用方Python模块(self):
        workflow = yaml.safe_load((ROOT.parents[2]/'.github/workflows/documentation-repair-screening.yml').read_text(encoding='utf-8'))
        step = next(s for s in workflow['jobs']['screen']['steps'] if s.get('id') == 'value')
        command = step['run']
        self.assertEqual(command, 'python -I "$GITHUB_WORKSPACE/.github/scripts/pr_gov/pr_checker.py"')
        bash = str(Path(os.environ.get('ProgramFiles', 'C:/Program Files'), 'Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash')
        with tempfile.TemporaryDirectory(prefix='pr triage caller space ') as temp:
            Path(temp, 'json.py').write_text('raise RuntimeError("untrusted module")', encoding='utf-8')
            data = Path(temp, 'input.json')
            data.write_text(dumps(sample()), encoding='utf-8')
            env = {**os.environ, 'PYTHONPATH': temp, 'GITHUB_WORKSPACE': ROOT.parents[2].as_posix(), 'RUNNER_TEMP': temp,
                   'INPUT_FILES-JSON': str(data), 'PATH': str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH']}
            result = subprocess.run([bash, '-c', command], cwd=temp, env=env, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('uncertain', result.stdout)


if __name__ == '__main__': unittest.main()
