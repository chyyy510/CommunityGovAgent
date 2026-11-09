"""采集公开 GitCode PR 的完整快照；不保存作者信息，不执行仓库文件。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from src.common import dumps, valid_path
from src.patch import parse_patch, patch_matches_head

API = 'https://api.gitcode.com/api/v5/repos/cann/cann-learning-hub'
REPOSITORY = 'https://gitcode.com/cann/cann-learning-hub'
SELECTION = {
    1407: ('candidate', '文字重复、缺字和量词修复，预期进入模型判断。'),
    1242: ('candidate', '实际差异是将数值单位的 LaTeX 表述改为普通文本，不以标题中的重复标点描述代替 diff。'),
    1405: ('uncertain', '外链目标修正；当前输入没有能证明链接正确的事实参考。'),
    208: ('uncertain', '命令名与标题修正；没有参考材料时不能证明命令修复正确。'),
    528: ('uncertain', '同时修改 Markdown、Notebook 并重命名图片，必须按完整 PR 跳过。'),
    1135: ('uncertain', '仅修复 shell 脚本换行，不属于文档修复。'),
    1380: ('uncertain', '多篇博客标题层级修复，用于检查整份 PR 的改动数量上限。'),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def get(path):
    with urlopen(Request(API + path, headers={'User-Agent': 'TrueContribution-public-fixtures'}), timeout=30) as response:
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError('公开 API 响应超过采集上限')
    return json.loads(raw)


def paged(path):
    rows = []
    for page in range(1, 21):
        chunk = get(f'{path}?per_page=100&page={page}')
        if not isinstance(chunk, list):
            raise ValueError('分页接口未返回列表')
        rows.extend(chunk)
        # 此平台的小结果集可能忽略 page；完整性还必须由未截断的 compare 验证。
        if len(chunk) < 100:
            return rows, page
    raise ValueError('分页超过采集上限')


def normalize(file):
    patch = file.get('patch')
    if isinstance(patch, dict):
        if patch.get('too_large'):
            raise ValueError('PR 差异被截断')
        status = ('added' if patch.get('new_file') else 'removed' if patch.get('deleted_file')
                  else 'renamed' if patch.get('renamed_file') else 'modified')
        text = patch['diff']
    else:
        status, text = file['status'], patch or ''
    if file.get('truncated'):
        raise ValueError('比较差异被截断')
    if not valid_path(file['filename']):
        raise ValueError('文件路径无效')
    return dict(filename=file['filename'], status=status, additions=file['additions'],
                deletions=file['deletions'], patch=text if status == 'modified' else '')


def signature(files):
    return sorted((normalize(file) for file in files), key=lambda file: file['filename'])


def matches_compare(pr_files, compare_files):
    expected, observed = signature(pr_files), signature(compare_files)
    if len(expected) != len(observed):
        return False
    for left, right in zip(expected, observed):
        # GitCode compare 把无文本 diff 的图片重命名标为 modified；后续用两端 blob 校验。
        if (left['status'] == 'renamed' and right['status'] == 'modified'
                and left['additions'] == left['deletions'] == 0 and not right['patch']):
            right = {**right, 'status': 'renamed'}
        if left != right:
            return False
    return True


def content(path, revision):
    value = get('/contents/' + quote(path, safe='/') + '?ref=' + revision)
    if value.get('encoding') != 'base64' or value.get('type') != 'file':
        raise ValueError('固定版本正文不可用')
    raw = base64.b64decode(value['content'].replace('\n', '').replace('\r', ''), validate=True)
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if blob != value['sha'] or len(raw) != value['size'] or len(raw) > 500_000:
        raise ValueError('固定版本正文哈希或大小不匹配')
    return raw, dict(blobSha=blob, sha256=digest(raw), bytes=len(raw))


def verify_patch(before, after, patch):
    parsed = parse_patch(patch)
    if not patch_matches_head(after, parsed):
        raise ValueError('补丁与 head 正文不一致')
    lines = before.split('\n')
    for block in reversed(parsed['blocks']):
        if not block['removed']:
            raise ValueError('当前选定语料应为文字替换')
        start = block['removed'][0]['line'] - 1
        old = [line['text'] for line in block['removed']]
        if lines[start:start + len(old)] != old:
            raise ValueError('补丁与 base 正文不一致')
        lines[start:start + len(old)] = [line['text'] for line in block['added']]
    if '\n'.join(lines) != after:
        raise ValueError('补丁未完整覆盖 base 到 head 的修改')
    return parsed


def collect(number):
    pr = get(f'/pulls/{number}')
    head = pr['head']['sha']
    files, file_pages = paged(f'/pulls/{number}/files')
    commits, commit_pages = paged(f'/pulls/{number}/commits')
    commit_ids = {commit['sha'] for commit in commits}
    if head not in commit_ids:
        raise ValueError('PR 提交列表没有 head')
    boundaries = sorted({sha for commit in commits for sha in commit['parents']['shas']} - commit_ids)
    # PR 的 base.sha 是当前分支 tip；以完整 compare 与 PR 文件逐项相同的历史父提交作比较基线。
    selected = None
    for boundary in boundaries:
        compare = get(f'/compare/{boundary}...{head}')
        if compare.get('truncated') is False and matches_compare(files, compare['files']):
            selected = compare['merge_base_commit']['sha']
            break
    if selected is None:
        raise ValueError(f'PR {number} 未找到与完整差异一致的历史基线')
    rows, proofs = [], []
    for row in signature(files):
        record = dict(row)
        proof = dict(filename=row['filename'], patchSha256=digest(row['patch'].encode('utf-8')))
        if row['status'] == 'modified':
            old_raw, old_proof = content(row['filename'], selected)
            new_raw, new_proof = content(row['filename'], head)
            before, after = old_raw.decode('utf-8'), new_raw.decode('utf-8')
            parsed = verify_patch(before, after, row['patch'])
            if (parsed['additions'], parsed['deletions']) != (row['additions'], row['deletions']):
                raise ValueError('补丁计数与 API 不一致')
            record.update(baseContent=before, headContent=after, sha=new_proof['blobSha'])
            proof.update(base=old_proof, head=new_proof, patchReconstructsHead=True)
        else:
            original = next(file for file in files if file['filename'] == row['filename'])
            if row['status'] == 'renamed':
                previous = original['patch']['old_path']
                old_raw, old_proof = content(previous, selected)
                new_raw, new_proof = content(row['filename'], head)
                if old_raw != new_raw:
                    raise ValueError('图片重命名前后内容不同')
                record['previous_filename'] = previous
                proof.update(base=old_proof, head=new_proof, renameBlobUnchanged=True)
            # 保留完整文件列表和重命名证明，不把二进制图片放入模型输入。
            proof['contentOmitted'] = 'non_modified_file_metadata_is_sufficient_for_scope_gate'
        rows.append(record)
        proofs.append(proof)
    again = get(f'/pulls/{number}')
    again_files, _ = paged(f'/pulls/{number}/files')
    if again['head']['sha'] != head or signature(again_files) != signature(files):
        raise ValueError('采集过程中 PR 快照改变')
    data = dict(schemaVersion=1, complete=True, title=pr['title'], body=pr.get('body') or '', files=rows,
                source=dict(provider='atomgit', baseSha=selected, headSha=head), references=[])
    expected, note = SELECTION[number]
    provenance = dict(number=number, title=pr['title'], url=f'{REPOSITORY}/pull/{number}',
        state=pr['state'], headSha=head, comparisonBaseSha=selected,
        observedTargetBranchTip=pr['base']['sha'], baseVerification='complete_compare_matches_pr_diff',
        fileCount=len(files), fileListPages=file_pages, commitPages=commit_pages,
        commitShas=sorted(commit_ids), snapshotStable=True, files=proofs,
        inputSha256=digest(dumps(data).encode('utf-8')))
    print(dumps(dict(pr=number, files=len(rows), base=selected, verified=True)), flush=True)
    return dict(id=f'gitcode-cann-learning-hub-{number}', expected=expected, note=note, input=data), provenance


def main():
    paths = [HERE / name for name in ('cases.json', 'sources.json', 'UPSTREAM-LICENSE.txt')]
    if any(path.exists() for path in paths):
        raise ValueError('固定语料已存在；需要更新时请先明确选择新的语料目录')
    with ThreadPoolExecutor(max_workers=3) as executor:
        collected = list(executor.map(collect, SELECTION))
    license_revision = collected[0][1]['headSha']
    license_raw, license_proof = content('LICENSE', license_revision)
    cases = dict(schemaVersion=1, samples=[row[0] for row in collected])
    sources = dict(schemaVersion=1, repository=REPOSITORY, collectedAt=datetime.now(timezone.utc).isoformat(),
        license=dict(path='LICENSE', revision=license_revision, **license_proof),
        casesSha256=digest((dumps(cases, indent=2) + '\n').encode('utf-8')),
        samples=[row[1] for row in collected])
    paths[0].write_text(dumps(cases, indent=2) + '\n', encoding='utf-8', newline='\n')
    paths[1].write_text(dumps(sources, indent=2) + '\n', encoding='utf-8', newline='\n')
    paths[2].write_bytes(license_raw)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
