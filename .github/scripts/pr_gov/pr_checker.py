"""读取 PR 输入、调用模型判断并生成评论正文；评论发布由后续工作流步骤负责。"""

from pathlib import Path
import os
import sys

# 工作流用 python -I 启动，不会自动搜索脚本目录；这里加入该目录，才能导入旁边的 src。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.assessment import assess, uncertain
from src.common import read_json_file, JsonFileError
from src.comments import render_comment
from src.model import read_model_config


def run_check(*, env=None, **model_dependencies):
    """执行一次检查；测试可传入模拟环境和模型调用，避免访问真实服务。"""
    env = dict(os.environ if env is None else env)

    def path(value):
        # 相对路径优先以仓库工作区为基准；本地未设置工作区时使用当前目录。
        location = Path(value)
        return location if location.is_absolute() else Path(env.get('GITHUB_WORKSPACE') or Path.cwd()) / location

    # 指定了配置文件就优先读取；否则使用工作流通过 LLM_MODEL 设置的 DeepSeek 模型。
    config_path = env.get('INPUT_MODEL-CONFIG')
    config = read_model_config(path(config_path)) if config_path else None
    if config is None and 'LLM_MODEL' in env:
        config = {
            'mode': 'api', 'runtime': 'ci', 'allowExternalProcessing': True,
            'material': 'public', 'timeoutMs': 60000,
            'api': {'endpoint': 'https://api.deepseek.com/chat/completions',
                    'model': env['LLM_MODEL'] or 'deepseek-flash', 'keyEnv': 'VALUE_MODEL_API_KEY',
                    'maxOutputTokens': 8192, 'thinking': 'disabled'},
        }

    # 输入由 read_pr.cjs 或本地测试提供。读取上限为 1 MiB，失败时不调用模型。
    try:
        data = read_json_file(path(env['INPUT_FILES-JSON']), 1_048_576)
    except KeyError:
        result = uncertain('input_unavailable')
    except JsonFileError as error:
        result = uncertain({'size_limit': 'input_size_limit', 'invalid_json': 'invalid_input_json'}.get(str(error), 'input_unavailable'))
    else:
        # assess 依次筛标题、检查输入、调用模型分类，再判断修复是否正确。
        result = assess(data, config=config, env=env, **model_dependencies)

    # 所有结果都生成评论；本地未设置 COMMENT_PATH 时不写文件。
    if env.get('COMMENT_PATH'):
        comment_path = path(env['COMMENT_PATH'])
        # 清理旧正文，避免本次写入失败后误发上次结果。
        comment_path.unlink(missing_ok=True)

        comment_path.write_text(render_comment(result), encoding='utf-8', newline='\n')

    return result


def main():
    """输出简短状态；退出码 0 表示检查流程完成，并不代表建议合并。"""
    try:
        result = run_check()
        print(f"TrueContribution: {result['status']}; {result['recommendation']}")
        return 0
    except Exception:
        # 未处理异常令步骤失败；不打印可能包含密钥或接口原文的异常详情。
        print('PR 检查未完成，请检查模型配置、提示词及评论模板。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
