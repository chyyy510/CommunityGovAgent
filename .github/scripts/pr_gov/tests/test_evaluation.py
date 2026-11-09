"""本地测试的模型调用和结果统计；模拟回复不代表真实模型效果。"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from test_assessment import sample, reply, CONFIG

spec = importlib.util.spec_from_file_location('real_pr_runner', Path(__file__).resolve().parent/'real_pr/cann-learning-hub/run.py')
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)


class EvaluationTests(unittest.TestCase):
    def test_每次测试都重新调用模型并使用本次回复(self):
        cases = dict(schemaVersion=1, samples=[dict(id='typo', expected='candidate', input=sample())])
        invoke = Mock(side_effect=reply())
        first = runner.evaluate(cases, config=CONFIG, env={}, invoke=invoke)
        self.assertEqual(first['summary']['matches'], 1)
        self.assertEqual(first['execution'], 'live')
        invoke.side_effect = None
        invoke.return_value = '{}'
        second = runner.evaluate(cases, config=CONFIG, env={}, invoke=invoke)
        self.assertEqual(invoke.call_count, 4)
        self.assertEqual(second['summary']['modelCompleted'], 0)
        self.assertEqual(second['samples'][0]['report']['reasons'][0]['code'], 'model_invalid_response')

    def test_离线模式明确区分未运行模型和漏选(self):
        result = runner.evaluate(dict(schemaVersion=1, samples=[dict(id='typo', expected='candidate', input=sample())]))
        self.assertEqual(result['execution'], 'offline')
        self.assertEqual(result['summary']['modelCompleted'], 0)
        self.assertEqual(result['summary']['missedRepairs'], 1)

    def test_拒绝重复样例(self):
        cases = dict(schemaVersion=1, samples=[dict(id='typo', expected='candidate', input=sample())])
        cases['samples'] *= 2
        with self.assertRaises(ValueError): runner.evaluate(cases)
