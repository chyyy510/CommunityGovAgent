"""从 pr_gov 目录读取提示词和模板，无论从哪个目录启动脚本都读取同一份文件。"""

import json
import re
from pathlib import Path
from string import Template

# 固定到 pr_gov 目录，避免从启动目录误读同名模板或提示词。
ROOT = Path(__file__).resolve().parents[1]


def escape(value):
    # 转义 *、< 等特殊字符，让文件名和模型解释按普通文字显示，不影响评论排版。
    return re.sub(r'[&<>`*_[\]{}()#!|\\~\r\n]', lambda m: '&#%d;' % ord(m[0]), str(value))


def read_text(relative_path):
    return (ROOT / relative_path).read_text(encoding='utf-8')


def read_messages():
    # 从 messages.json 读取评论用语，包括错误说明、判断结果的中文名称和段落模板。
    return json.loads(read_text('templates/messages.json'))


def substitute(text, **values):
    # 用传入的值替换模板中的 $name 等占位符；值中即使含有 $name，也保留原样。
    return Template(text).substitute(values)


def render_template(name, **values):
    # 只去除文件结尾的换行，段落内的空白由模板决定。
    return substitute(read_text('templates/' + name).rstrip('\n'), **values)
