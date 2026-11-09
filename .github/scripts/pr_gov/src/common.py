"""提供输入检查和 JSON 读写函数，仅使用 Python 标准库。"""

import json
import math
import re
from functools import lru_cache

I = re.I


@lru_cache(maxsize=128)
def rx(pattern, flags=0): return re.compile(pattern, flags)


def byte_len(value): return len(value.encode('utf-8', errors='surrogatepass'))


def dumps(value, **kwargs):
    # 中文保持原样，无法直接编码为 UTF-8 的代理字符写成 \uXXXX；不允许 NaN 或无穷值。
    text = json.dumps(value, ensure_ascii=False, separators=(',', ': ' if kwargs.get('indent') else ':'), allow_nan=False, **kwargs)
    return re.sub(r'[\ud800-\udfff]', lambda m: '\\u%04x' % ord(m[0]), text)


def loads(text):
    # 同名字段重复出现时直接报错，避免后面的值覆盖前面的值；也不接受 NaN 或无穷值。
    def invalid(_): raise ValueError('invalid_json')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result): raise ValueError('invalid_json_number')
        return result

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)


def integer(value, minimum=0, maximum=9007199254740991):
    # 不把 True/False 当作行号或数量；默认上限是 JavaScript 能精确表示的最大安全整数。
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value) and int(value) == value


def obj(value): return isinstance(value, dict)


def keys(value, required):
    # 字段集合必须完全一致，多余或缺失字段都不接受。
    return obj(value) and value.keys() == set(required)


def get(value, *path):
    # 逐层读取可选字段，中间不是字典或字段不存在时返回 None。
    for key in path:
        if not isinstance(value, dict): return None
        value = value.get(key)
    return value


def valid_path(value):
    # 路径必须相对于仓库根目录，使用 / 分隔；不接受绝对路径、.、.. 或控制字符。
    return isinstance(value, str) and 0 < len(value) <= 1024 and not re.search(r'[\x00-\x1f\x7f\\:\ud800-\udfff]', value) and not value.startswith('/') and not any(p in ('', '.', '..') for p in value.split('/'))


def controls(value): return bool(re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]', value))


class JsonFileError(Exception): pass


def read_json_file(path, max_bytes):
    # 多读一个字节判断是否超限，不先将整个文件载入内存；兼容带 BOM 的 UTF-8。
    try:
        with open(path, 'rb') as stream: data = stream.read(max_bytes + 1)
        if len(data) > max_bytes: raise JsonFileError('size_limit')
    except JsonFileError: raise
    except (OSError, TypeError, ValueError): raise JsonFileError('unavailable') from None

    try: return loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError): raise JsonFileError('invalid_json') from None
