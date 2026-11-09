"""解析单个文件的 diff 补丁，检查行数和内容是否与修改后的完整文件一致。"""

from .common import rx, integer


def patch_matches_head(content, parsed):
    # 将补丁中的新增行和未修改行逐行对照完整正文；删除行不在修改后的文件中。
    lines = content.split('\n')
    if lines[-1] == '':
        lines.pop()
    if any(item['line'] < 1 or item['line'] > len(lines) or lines[item['line'] - 1] != item['text'] for item in parsed['newLines']):
        return False

    # 末尾是否有换行也是补丁内容的一部分，需要与正文保持一致。
    last = parsed['newLines'][-1]['line'] if parsed['newLines'] else None
    if parsed['newNoNewline'] and last != len(lines):
        return False
    return not (last == len(lines) and content.endswith('\n') == parsed['newNoNewline'])


def parse_patch(patch):
    # hunk 是 @@ 标记的补丁段；block 是其中一组连续的新增或删除行。
    lines = patch.split('\n')
    if lines[-1] == '':
        lines.pop()

    out = dict(blocks=[], newLines=[], additions=0, deletions=0, oldNoNewline=False, newNoNewline=False)
    hunk = block = last_kind = None
    last_content = False

    def finish_block():
        nonlocal block
        if block is not None:
            out['blocks'].append(block)
        block = None

    def finish_hunk():
        finish_block()
        # 实际行数必须与 @@ 声明一致，缺行和多行都视为无效补丁。
        if hunk and (hunk['oldSeen'] != hunk['oldCount'] or hunk['newSeen'] != hunk['newCount']):
            raise ValueError('invalid_patch')

    for line in lines:
        header = rx(r'^@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?$').fullmatch(line)
        if header:
            finish_hunk()
            previous = hunk
            old_start, old_count, new_start, new_count = [int(x if x is not None else 1) for x in header.groups()]

            # 行数为 0 时，起点指向空缺位置的前一行；加 1 后用于计算后续行号。
            old_begin, new_begin = old_start + (old_count == 0), new_start + (new_count == 0)
            old_gap = old_begin - (previous['oldBegin'] + previous['oldCount'] if previous else 1)
            new_gap = new_begin - (previous['newBegin'] + previous['newCount'] if previous else 1)

            # 相邻补丁段之间未修改的行数应相同，区间不能倒退或重叠。
            if not all(integer(n) for n in (old_start, old_count, new_start, new_count)) or (old_count and old_start == 0) or (new_count and new_start == 0) or not (old_count or new_count) or old_gap < 0 or new_gap < 0 or old_gap != new_gap:
                raise ValueError('invalid_patch')

            hunk = dict(oldStart=old_start, oldCount=old_count, newStart=new_start, newCount=new_count, oldBegin=old_begin, newBegin=new_begin, oldSeen=0, newSeen=0)
            last_content = False
            continue

        if hunk is None:
            raise ValueError('invalid_patch')

        # 此标记表示上一行是文件末行且没有换行符，标记本身不计入增删行数。
        if line == '\\ No newline at end of file' and last_content:
            if last_kind != '+': out['oldNoNewline'] = True
            if last_kind != '-': out['newNoNewline'] = True
            last_content = False
            continue

        kind = line[:1]
        if kind not in (' ', '+', '-') or (kind != '+' and out['oldNoNewline']) or (kind != '-' and out['newNoNewline']):
            raise ValueError('invalid_patch')
        last_content, last_kind = True, kind
        old_line, new_line = hunk['oldBegin'] + hunk['oldSeen'], hunk['newBegin'] + hunk['newSeen']

        # 未修改的行在新旧文件中都要计数；新增行只计入新文件，删除行只计入旧文件。
        if kind != '+': hunk['oldSeen'] += 1
        if kind != '-': hunk['newSeen'] += 1
        if kind != '-': out['newLines'].append(dict(line=new_line, text=line[1:]))
        if hunk['oldSeen'] > hunk['oldCount'] or hunk['newSeen'] > hunk['newCount']:
            raise ValueError('invalid_patch')

        if kind == ' ':
            finish_block()
        else:
            if block is None: block = dict(removed=[], added=[], oldStart=old_line, newStart=new_line)
            if kind == '-':
                block['removed'].append(dict(text=line[1:], line=old_line))
                out['deletions'] += 1
            else:
                block['added'].append(dict(text=line[1:], line=new_line))
                out['additions'] += 1

    finish_hunk()
    if hunk is None or out['additions'] + out['deletions'] == 0:
        raise ValueError('invalid_patch')
    return out
