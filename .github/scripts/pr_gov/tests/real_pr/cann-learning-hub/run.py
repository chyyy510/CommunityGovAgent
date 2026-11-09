"""测试真实 PR：默认离线检查，--live 调用 DeepSeek，结果保存为本地报告。"""
import argparse
from datetime import datetime, timezone
import hashlib
import time
import os
from pathlib import Path
import shlex
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from src.common import dumps, read_json_file
from src.comments import render_comment
from src.assessment import assess
from src.model import invoke_model


def load_cases():
    # 旧样例只保存了标题，没有 PR 描述；运行时补入标题，描述留空，不编造内容。
    cases = read_json_file(HERE / 'cases.json', 4_000_000)
    sources = read_json_file(HERE / 'sources.json', 4_000_000)
    titles = {f"gitcode-cann-learning-hub-{row['number']}": row['title'] for row in sources['samples']}
    for case in cases['samples']:
        case['input'].setdefault('title', titles[case['id']])
        case['input'].setdefault('body', '')
    return cases


def evaluate(cases, *, config=None, invoke=invoke_model, env=None):
    if (not isinstance(cases, dict) or cases.get('schemaVersion') != 1 or not isinstance(cases.get('samples'), list)
        or not 1 <= len(cases['samples']) <= 200): raise ValueError('invalid_cases')
    ids = set()
    for case in cases['samples']:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'] or case['id'] in ids
            or case.get('expected') not in ('candidate', 'uncertain') or not isinstance(case.get('input'), dict)):
            raise ValueError('invalid_case')
        ids.add(case['id'])
    results = []
    for case in cases['samples']:
        started = time.monotonic()
        result = assess(case['input'], config=config, env=env, invoke=invoke)
        results.append(dict(id=case['id'], expected=case['expected'], actual=result['status'],
            matches=result['status'] == case['expected'], elapsedSeconds=round(time.monotonic()-started, 3), report=result))
    false_candidates = sum(r['expected'] == 'uncertain' and r['actual'] == 'candidate' for r in results)
    missed_repairs = sum(r['expected'] == 'candidate' and r['actual'] != 'candidate' for r in results)
    return dict(schemaVersion=1, execution='live' if config and config != {'mode': 'off'} else 'offline',
        createdAt=datetime.now(timezone.utc).isoformat(),
        sourceSha256={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted([*ROOT.glob('*.py'), ROOT/'src/title_rules.json', *(ROOT/'src').rglob('*.py'), *(ROOT/'src').rglob('*.cjs'),
                         *(ROOT/'prompts').glob('*.md'), *(ROOT/'templates').glob('*.md'),
                         *(ROOT/'templates').glob('*.json')])},
        summary=dict(samples=len(results), matches=sum(r['matches'] for r in results), falseCandidates=false_candidates,
            missedRepairs=missed_repairs, modelCompleted=sum(r['report']['modelReview']['status']=='completed' for r in results)), samples=results)


def model_environment(env_file):
    env = dict(os.environ)
    allowed = {'DEEPSEEK_API_KEY', 'VALUE_MODEL_API_KEY', 'LLM_MODEL'}
    if env_file.exists():
        for raw in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, separator, value = line.partition('=')
            key = key.strip()
            if not separator or key not in allowed or env.get(key):
                continue
            parts = shlex.split(value, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError('模型环境变量格式无效；含空格的值需要引号')
            if parts:
                env[key] = parts[0]
    env['VALUE_MODEL_API_KEY'] = env.get('VALUE_MODEL_API_KEY') or env.get('DEEPSEEK_API_KEY', '')
    if not env['VALUE_MODEL_API_KEY'].strip():
        raise ValueError('没有可用的 DeepSeek 测试密钥')
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--live', action='store_true', help='调用真实 DeepSeek API，可能产生费用')
    parser.add_argument('--env-file', type=Path, default=ROOT.parents[2] / '.env', help='仅加载模型相关变量，不输出密钥')
    parser.add_argument('--output-dir', required=True, type=Path, help='新报告目录；拒绝覆盖已有结果')
    options = parser.parse_args()
    cases = load_cases()
    env, config = {}, None
    if options.live:
        env = model_environment(options.env_file)
        config = dict(mode='api', runtime='local', allowExternalProcessing=True, material='public', timeoutMs=120000,
            api=dict(endpoint='https://api.deepseek.com/chat/completions', model=env.get('LLM_MODEL') or 'deepseek-flash',
                     keyEnv='VALUE_MODEL_API_KEY', maxOutputTokens=8192, thinking='disabled'))
    options.output_dir.mkdir(parents=True, exist_ok=False)
    calls = 0
    def invoke(messages, selected, **kwargs):
        nonlocal calls
        calls += 1
        print(dumps(dict(modelRequest=calls, model=selected['api']['model'])), flush=True)
        return invoke_model(messages, selected, **kwargs)
    report = evaluate(cases, config=config, env=env, invoke=invoke)
    report['modelRequests'] = calls
    report['platformWrites'] = 0
    (options.output_dir / 'report.json').write_text(dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    for row in report['samples']:
        result = row['report']
        # 所有结果都生成本地评论预览，绝不调用平台评论接口。
        (options.output_dir / (row['id'] + '.md')).write_text(render_comment(result) + '\n', encoding='utf-8', newline='\n')
        print(dumps(dict(id=row['id'], expected=row['expected'], actual=row['actual'],
                        screening=result.get('screening'), model=result['modelReview']['status'],
                        reasons=[reason['code'] for reason in result['reasons']])), flush=True)
    print(dumps(dict(execution=report['execution'], modelRequests=calls, platformWrites=0, **report['summary'])))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except Exception as error:
        # 不打印包含文件内容或 HTTP 响应的异常，避免把本地配置带入日志。
        print('真实 PR 测试未完成，请检查输入、密钥配置及新的输出目录。错误类型：' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
