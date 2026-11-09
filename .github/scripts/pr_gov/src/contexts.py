"""按文件和版本合并上下文行号区间，正文内容不参与去重。"""

from .common import rx


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if start >= end: continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def headings(lines):
    # 轻量提取 Markdown ATX / Setext 标题，忽略围栏代码；只用于补充原文。
    result, fence = [], None
    for index, line in enumerate(lines):
        marker = rx(r'^ {0,3}(`{3,}|~{3,})(.*)$').match(line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        atx = rx(r'^ {0,3}(#{1,6})(?:\s|$)').match(line)
        if atx:
            result.append((index, index + 1, len(atx[1])))
        elif (index and lines[index - 1].strip() and
              rx(r'^ {0,3}(?:=+|-+)\s*$').fullmatch(line) and
              not rx(r'^\s*(?:[#>`~]|[-*+]\s)').match(lines[index - 1])):
            result.append((index - 1, index + 1, 1 if line.lstrip()[0] == '=' else 2))
    return result


def heading_context(outline, start, end):
    # 保留改动前的标题祖先链、紧邻标题和改动后的首个标题，不带中间正文。
    parents, following = [], None
    for heading in outline:
        first, last, level = heading
        if last <= start:
            while parents and parents[-1][2] >= level: parents.pop()
            parents.append(heading)
        elif first >= end:
            following = heading
            break
    return [(h[0], h[1]) for h in [*parents, following] if h is not None]


def heading_level_only(change):
    # 仅改变标题标记时采用标题片段；同时修改文字或正文仍保留普通上下文窗口。
    def title_lines(text):
        result = []
        for line in text.split('\n'):
            atx = rx(r'^( {0,3})#{1,6}((?:\s.*)?)$').fullmatch(line)
            if atx: result.append(atx[1] + atx[2])
            elif rx(r'^ {0,3}(?:=+|-+)\s*$').fullmatch(line): result.append('')
            else: return None
        return result
    before, after = title_lines(change['before']), title_lines(change['after'])
    return before is not None and before == after


def shared_files(inspection):
    files = {}
    for change in inspection['changes']:
        file = files.setdefault(change['path'], dict(path=change['path'], changes=[], contexts=[]))
        file['changes'].append({**change, 'contextIds': dict(base=[], head=[])})

    for path, file in files.items():
        documents = inspection['documents'][path]
        outlines = {version: headings(lines) for version, lines in documents.items()}
        structural = set()
        for change in file['changes']:
            for version, line_key, count_key in (('base', 'oldLine', 'oldCount'), ('head', 'newLine', 'newCount')):
                start = change[line_key] - 1
                end = start + change[count_key]
                if any(first < end and last > start for first, last, _ in outlines[version]):
                    structural.add(change['id'])

        for version, line_key, count_key in (('base', 'oldLine', 'oldCount'), ('head', 'newLine', 'newCount')):
            lines = documents[version]
            requested, changed = {}, []
            for change in file['changes']:
                start = change[line_key] - 1
                end = start + change[count_key]
                changed.append((start, end))
                ranges = [(max(0, start - 5), start), (end, min(len(lines), end + 5))]
                if change['id'] in structural:
                    if heading_level_only(change): ranges = []
                    # Setext 的标题文字与下划线可能分属 change 和 context。
                    ranges.extend((first, last) for first, last, _ in outlines[version] if first < end and last > start)
                    ranges.extend(heading_context(outlines[version], start, end))
                requested[change['id']] = merge_ranges(ranges)

            # change 的完整原文已经发送，从共享区间中扣除本批所有 change 的对应行。
            ranges = merge_ranges([r for group in requested.values() for r in group])
            for first, last in merge_ranges(changed):
                ranges = [(a, b) for start, end in ranges for a, b in
                          ((start, min(end, first)), (max(start, last), end)) if a < b]
            for start, end in ranges:
                context_id = f'{version}-{start + 1}-{end}'
                file['contexts'].append(dict(id=context_id, version=version, startLine=start + 1,
                                             endLine=end, text='\n'.join(lines[start:end])))
                for change in file['changes']:
                    if any(first < end and last > start for first, last in requested[change['id']]):
                        change['contextIds'][version].append(context_id)
    return list(files.values())
