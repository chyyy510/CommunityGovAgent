"""判断步骤、评论正文及官方 Action 之间的数据交接测试。"""
import os
import json
import copy
from html import unescape
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import yaml
from test_assessment import sample, CONFIG, reply
from test_integration import ROOT
from pr_checker import run_check
from src.assessment import assess
from src.comments import MARKER, render_comment
from src.common import dumps


def assessed(**kwargs):
    result = assess(sample(), config=CONFIG, env={}, invoke=reply(**kwargs))
    result['source'] = dict(baseSha='a'*40, headSha='b'*40)
    return result


class CommentTests(unittest.TestCase):
    def test_全部改善时总结共同目的且完整依据默认折叠(self):
        data = sample()
        data['files'].append(sample(path='docs/other.md')['files'][0])
        invoke = Mock(wraps=reply())
        result = assess(data, config=CONFIG, env={}, invoke=invoke)
        result['source'] = dict(baseSha='a'*40, headSha='b'*40)
        result['classification']['reason'] = '针对文档中的拼写错误，统一将 recieve 改为 receive。'
        original = copy.deepcopy(result)
        body = render_comment(result)
        visible, detail = body.split('<details>', 1)
        detail, footer = detail.split('</details>', 1)
        self.assertTrue(body.startswith(MARKER))
        self.assertIn('### 总体结论', visible)
        self.assertIn('**建议合并**', visible)
        self.assertIn('此建议仅针对文档修复，不代表 PR 已获批准；是否合并由维护者决定。', visible)
        self.assertIn('涉及 2 个文件，修改项数：2；增删合计 4 行', visible)
        summary = visible.split('### 审阅总结', 1)[1]
        self.assertIn('仅说明修改目的，不代表修复已验证', summary)
        self.assertIn(result['classification']['reason'], summary)
        self.assertIn('全部 2 处修改均修复了文档问题', summary)
        self.assertIn('语言表达调整', summary)
        self.assertIn('主要审阅要点（涉及 2 处修改）', visible)
        self.assertEqual(visible.count(result['evidence'][0]['reason']), 1)
        self.assertIn('<summary>查看全部 2 处修改的判断理由</summary>\n\n', detail)
        for item in result['evidence']:
            self.assertIn(f"{item['path']}:{item['newLine']}：修复正确。{item['reason']}", detail)
        self.assertIn('评估版本：head `' + 'b'*40 + '`；base `' + 'a'*40 + '`。', visible)
        self.assertIn('PR 更新后会重新检查；本评论只适用于上面的提交版本。', footer)
        self.assertEqual(result, original)
        self.assertEqual(invoke.call_count, 2)

    def test_各类异常的数量和主要问题保持可见(self):
        for kind, label, reason in (
            ('unchanged', '未见改善', '只替换同义词，原有歧义仍然存在。'),
            ('harmful', '可能引入错误', '删除了“不”，导致原意反转。'),
            ('uncertain', '无法确认', '缺少目标版本的说明，无法确认参数名称。'),
        ):
            with self.subTest(assessment=kind):
                result = assessed(assessment=kind)
                result['evidence'][0]['reason'] = reason
                body = render_comment(result)
                visible, detail = body.split('<details>', 1)
                self.assertIn('**暂不建议合并**', visible)
                self.assertIn('另有 1 处异常需要维护者关注', visible)
                self.assertIn(f'{label}：1 处。主要问题：{reason}', visible)
                self.assertIn(reason, detail)
                self.assertNotIn('全部 1 处修改均修复', visible)

    def test_混合结果汇总全部异常且重复问题不逐项展开(self):
        result = assessed(assessment='harmful')
        prototype = result['evidence'][0]
        rows = [
            ('improved', '修正了拼写。'),
            ('unchanged', '原有歧义仍然存在。'),
            ('harmful', '删除了必要的限制条件。'),
            ('harmful', '删除了必要的限制条件。'),
            ('harmful', '参数名称与正文不一致。'),
            ('uncertain', '缺少版本依据。'),
        ]
        result['evidence'] = [dict(prototype, id=f'change-{i}', newLine=i,
            assessment=kind, reason=reason) for i, (kind, reason) in enumerate(rows, 1)]
        result['evidence'][-1]['type'] = 'factual'
        visible, detail = render_comment(result).split('<details>', 1)
        self.assertIn('共审阅 6 处修改，其中 1 处被判为修复正确，另有 5 处异常', visible)
        self.assertIn('可能引入错误：3 处', visible)
        self.assertIn('未见改善：1 处', visible)
        self.assertIn('无法确认：1 处', visible)
        self.assertIn('语言表达调整、技术事实修改', visible)
        self.assertEqual(visible.count('删除了必要的限制条件。'), 1)
        self.assertIn('此类还有其他问题', visible)
        self.assertNotIn('参数名称与正文不一致。', visible)
        self.assertNotIn('修正了拼写。', visible)
        for item in result['evidence']:
            self.assertIn(f"{item['path']}:{item['newLine']}", detail)
            self.assertIn(item['reason'], detail)

    def test_审阅未完成不把分类目的当作修复事实(self):
        result = assessed()
        result['modelReview']['status'] = 'unavailable'
        result['evidence'] = []
        result['reasons'] = [dict(code='model_timeout')]
        body = render_comment(result)
        self.assertIn('涉及 1 个文件，修改项数：未确定；增删合计 2 行', body)
        self.assertIn('本次未完成有效的模型判断；这不表示修改本身有误。', body)
        self.assertIn('模型请求超时', body)
        self.assertIn('仅说明修改目的，不代表修复已验证', body)
        self.assertNotIn('全部 0 处', body)
        self.assertNotIn('<details>', body)

    def test_没有详细依据时不生成空折叠或宣称全部改善(self):
        for result in (assessed(classification='other'), assessed(), {}):
            result['evidence'] = []
            result.pop('classification', None)
            body = render_comment(result)
            self.assertIn('### 审阅总结', body)
            self.assertIn('### 详细依据\n\n暂无逐项审阅依据。', body)
            self.assertNotIn('<details>', body)
            self.assertNotIn('<summary>', body)
            self.assertNotIn('全部 0 处', body)

    def test_总结和详细依据均转义且不能提前关闭折叠(self):
        result = assessed(assessment='harmful')
        reason = '</details>\n<details open> @someone **合并** $summary'
        result['classification']['reason'] = reason
        result['evidence'][0]['reason'] = reason
        body = render_comment(result)
        self.assertEqual(body.count('<details>'), 1)
        self.assertEqual(body.count('</details>'), 1)
        self.assertNotIn('<details open>', body)
        self.assertNotIn('@someone', body)
        self.assertNotIn('**合并**', body)
        self.assertEqual(body.count('$summary'), 3)

    def test_完整理由超出评论上限时仍拒绝生成(self):
        result = assessed()
        result['evidence'] *= 2000
        with self.assertRaisesRegex(ValueError, 'comment_size_limit'):
            render_comment(result)

    def test_评论转义HTML和提及并避免无证据合并建议(self):
        result = assessed()
        result['evidence'][0]['reason'] = '<script>alert(1)</script> @everyone ![x](url)'
        body = render_comment(result)
        self.assertNotIn('<script>', body)
        self.assertNotIn('@everyone', body)
        self.assertNotIn('![x](url)', body)
        self.assertIn('**建议合并**', body)
        for assessment in ('harmful', 'unchanged', 'uncertain'):
            self.assertIn('**暂不建议合并**', render_comment(assessed(assessment=assessment)))
        result['modelReview']['status'] = 'unavailable'
        self.assertNotIn('**建议合并**', render_comment(result))

    def run_step(self, data, *, invoke=None, configured=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        data['source'] = dict(baseSha='a'*40, headSha='b'*40)
        source, config, comment, output = [root / name for name in ('input.json', 'model.json', 'comment.md', 'outputs')]
        source.write_text(dumps(data), encoding='utf-8')
        config.write_text(dumps(CONFIG), encoding='utf-8')
        comment.write_text('旧正文不应被误发', encoding='utf-8')
        result = run_check(env={'INPUT_FILES-JSON': str(source), 'COMMENT_PATH': str(comment),
            'GITHUB_OUTPUT': str(output), **({'INPUT_MODEL-CONFIG': str(config)} if configured else {})},
            invoke=invoke or reply())
        self.assertEqual({p.name for p in root.iterdir()} - {'input.json', 'model.json', 'comment.md', 'outputs'}, set())
        self.assertFalse(output.exists())
        return result, comment

    def test_符合范围生成临时正文(self):
        result, comment = self.run_step(sample())
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(comment.read_text(encoding='utf-8'), render_comment(result))
        self.assertIn('b'*40, comment.read_text(encoding='utf-8'))

    def test_标题未命中范围外或非修复都回复跳过原因(self):
        for data, invoke in ((sample(path='app.py'), Mock(side_effect=AssertionError('不应调用模型'))),
                             (sample(title='fix: login error'), Mock(side_effect=AssertionError('不应调用模型'))),
                             (sample(), reply(change_type='other'))):
            result, comment = self.run_step(data, invoke=invoke)
            self.assertEqual(result['screening'], 'skipped')
            body = comment.read_text(encoding='utf-8')
            self.assertIn('未进行修复审阅', body)
            self.assertIn(result['reasons'][0]['code'], unescape(body))
            self.assertIn('b'*40, body)
            self.assertNotIn('**暂不建议合并**', body)

    def test_缺配置分类失败或不确定都回复原因(self):
        from src.model import ModelError
        for configured, invoke in ((False, None), (True, Mock(side_effect=ModelError('model_timeout'))),
                                   (True, reply(classification='uncertain'))):
            result, comment = self.run_step(sample(), invoke=invoke, configured=configured)
            self.assertEqual(result['status'], 'uncertain')
            body = comment.read_text(encoding='utf-8')
            self.assertIn('未进行修复审阅', body)
            self.assertIn(result['reasons'][0]['code'], unescape(body))
            if result.get('classification'):
                self.assertIn(result['classification']['reason'], body)

    def test_分类通过但修复检查超时仍回复原因(self):
        from src.model import ModelError
        def invoke(messages, selected, **kwargs):
            if set(json.loads(messages[1]['content'])) == {'title', 'body'}:
                return reply()(messages, selected, **kwargs)
            raise ModelError('model_timeout')
        result, comment = self.run_step(sample(), invoke=invoke)
        self.assertEqual(result['classification']['type'], 'documentation_repair')
        self.assertIn('未完成有效的模型判断', comment.read_text(encoding='utf-8'))

    def test_输入无法读取或损坏时也生成评论(self):
        with tempfile.TemporaryDirectory() as directory:
            source, comment = Path(directory, 'input.json'), Path(directory, 'comment.md')
            for content in (None, '{', ' ' * 1_048_577):
                if content is not None: source.write_text(content, encoding='utf-8')
                result = run_check(env={'INPUT_FILES-JSON': str(source), 'COMMENT_PATH': str(comment)})
                body = comment.read_text(encoding='utf-8')
                self.assertIn(result['reasons'][0]['code'], unescape(body))
                self.assertIn('未进行修复审阅', body)

    def test_分类理由按普通文本显示(self):
        result = assessed(classification='other')
        result['classification']['reason'] = '<script> @someone **approve**'
        body = render_comment(result)
        self.assertNotIn('<script>', body)
        self.assertNotIn('@someone', body)
        self.assertNotIn('**approve**', body)

    def test_正文写入失败令判断步骤失败(self):
        import pr_checker
        with patch.object(pr_checker, 'run_check', side_effect=OSError('private-key')), patch('builtins.print') as log:
            self.assertEqual(pr_checker.main(), 1)
            self.assertNotIn('private-key', str(log.call_args_list))


class WorkflowTests(unittest.TestCase):
    def test_工作流按顺序交接且密钥仅进入判断步骤(self):
        workflow = yaml.safe_load((ROOT.parents[2]/'.github/workflows/documentation-repair-screening.yml').read_text(encoding='utf-8'))
        self.assertEqual(workflow[True]['pull_request_target']['types'], ['opened', 'synchronize', 'reopened', 'edited'])
        self.assertEqual(workflow[True]['workflow_dispatch']['inputs']['pr_number'],
            {'description': '要重新审阅的 PR 编号', 'required': True, 'type': 'number'})
        self.assertEqual(workflow['concurrency'],
            {'group': 'value-screen-${{ github.event.pull_request.number || inputs.pr_number }}', 'cancel-in-progress': True})
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        job = workflow['jobs']['screen']
        steps = job['steps']
        self.assertEqual(steps[0]['with']['ref'], "${{ github.event_name == 'workflow_dispatch' && github.sha || github.event.pull_request.base.sha }}")
        self.assertFalse(steps[0]['with']['persist-credentials'])
        read = next(s for s in steps if s.get('id') == 'pr')
        check = next(s for s in steps if s.get('id') == 'value')
        publish = steps[-1]
        self.assertEqual(read['uses'], 'actions/github-script@v9')
        self.assertEqual(publish['uses'], 'actions/github-script@v9')
        self.assertIn("require('./.github/scripts/pr_gov/src/read_pr.cjs')", read['with']['script'])
        self.assertIn('await readPR({ github, context, core })', read['with']['script'])
        self.assertIn("require('./.github/scripts/pr_gov/src/publish_comment.cjs')", publish['with']['script'])
        self.assertIn('await publishComment({ github, context, core })', publish['with']['script'])
        self.assertEqual(check['if'], "steps.pr.outputs.ready == 'true'")
        self.assertEqual(publish['if'], "${{ !cancelled() && steps.pr.outputs.resolved == 'true' }}")
        self.assertEqual(read['env']['PR_SNAPSHOT'], '${{ runner.temp }}/documentation-pr-snapshot.json')
        self.assertEqual(publish['env']['PR_SNAPSHOT'], read['env']['PR_SNAPSHOT'])
        self.assertEqual(publish['env']['PR_READ_REASON'], '${{ steps.pr.outputs.reason }}')
        self.assertEqual(publish['env']['CHECK_OUTCOME'], '${{ steps.value.outcome }}')
        self.assertEqual(check['env']['INPUT_FILES-JSON'], read['env']['PR_INPUT'])
        self.assertNotIn('PR_INPUT', publish['env'])
        self.assertEqual(publish['env']['COMMENT_PATH'], check['env']['COMMENT_PATH'])
        self.assertNotIn('GH_TOKEN', check['env'])
        self.assertEqual(check['env']['VALUE_MODEL_API_KEY'], '${{ secrets.DEEPSEEK_API_KEY }}')
        self.assertEqual(check['timeout-minutes'], 3)
        self.assertNotIn('VALUE_MODEL_API_KEY', job.get('env', {}))
        self.assertNotIn('runner.', dumps(job.get('env', {})))
        self.assertFalse(any('actions/upload-artifact@' in s.get('uses', '') for s in steps))
        for step in (read, publish):
            self.assertNotIn('${{', step['with']['script'])
        self.assertEqual(check['env']['LLM_MODEL'], "${{ vars.LLM_MODEL || 'deepseek-flash' }}")
        self.assertNotIn('INPUT_MODEL-CONFIG', check['env'])

    def test_工作流模型配置直接来自环境变量(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, 'input.json')
            source.write_text(dumps(sample()), encoding='utf-8')
            for name in ('', 'deepseek-v4-pro'):
                invoke = Mock(wraps=reply())
                result = run_check(env={'INPUT_FILES-JSON': str(source), 'GITHUB_ACTIONS': 'true',
                    'LLM_MODEL': name}, invoke=invoke)
                self.assertEqual(result['status'], 'candidate')
                selected = invoke.call_args.args[1]
                self.assertEqual(selected['api']['model'], name or 'deepseek-flash')
                self.assertEqual(selected['api']['endpoint'], 'https://api.deepseek.com/chat/completions')
                self.assertEqual(selected['api']['thinking'], 'disabled')
                self.assertEqual(selected['timeoutMs'], 60000)
                self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_官方客户端调用回归(self):
        completed = subprocess.run(['node', '--test', str(ROOT/'tests/test_github.cjs')],
            env={**os.environ, 'TEST_PYTHON': sys.executable}, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == '__main__': unittest.main()
