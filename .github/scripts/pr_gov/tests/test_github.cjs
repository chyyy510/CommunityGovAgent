// 模拟 github-script 注入的官方客户端；不访问网络或写真实 PR。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const readPR = require('../src/read_pr.cjs');
const publishComment = require('../src/publish_comment.cjs');

const marker = '<!-- truecontribution:documentation-repair-review:v1 -->';
const source = { baseSha: 'a'.repeat(40), headSha: 'b'.repeat(40), provider: 'github' };
const snapshot = { title: 'docs: fix typo', body: 'Correct a spelling mistake in the guide.',
  base: { sha: source.baseSha }, head: { sha: source.headSha }, changed_files: 1, state: 'open' };
const raw = Buffer.from('# Guide\nFixed typo.\n');
const blob = { sha: createHash('sha1').update(`blob ${raw.length}\0`).update(raw).digest('hex'),
  size: raw.length, encoding: 'base64', content: raw.toString('base64') };
const file = { filename: 'docs/guide.md', status: 'modified', additions: 1, deletions: 1,
  patch: '@@ -1,2 +1,2 @@\n # Guide\n-Fixed tpyo.\n+Fixed typo.', sha: blob.sha };

async function setup(t, options = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'pr-gov-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const env = { PR_INPUT: path.join(directory, 'input.json'), PR_SNAPSHOT: path.join(directory, 'snapshot.json'),
    COMMENT_PATH: path.join(directory, 'comment.md'), CHECK_OUTCOME: 'success' };
  await fs.writeFile(env.PR_INPUT, JSON.stringify({ source }));
  await fs.writeFile(env.PR_SNAPSHOT, JSON.stringify({ ...snapshot, number: 7 }));
  await fs.writeFile(env.COMMENT_PATH, marker + '\n建议合并\n');
  const calls = [], outputs = {}, errors = [];
  let reads = 0;
  const context = { repo: { owner: 'sample', repo: 'project' }, issue: { number: 7 },
    eventName: 'pull_request_target', payload: { repository: { full_name: 'sample/project' }, pull_request: snapshot } };
  if (options.manual) {
    context.eventName = 'workflow_dispatch';
    context.payload = { inputs: { pr_number: '7' } };
    delete context.issue;
    context.sha = 'c'.repeat(40);
    env.GITHUB_SHA = context.sha;
  }
  const github = { rest: {
    pulls: { get: async args => {
      assert.deepEqual(args, { ...context.repo, pull_number: 7 });
      calls.push('get');
      if (options.getError) throw Object.assign(new Error('private-marker'), { status: options.getError });
      return { data: options.snapshots?.[reads++] || snapshot };
    }, listFiles: async args => {
      assert.deepEqual(args, { ...context.repo, pull_number: 7, per_page: 100 });
      calls.push('files');
      if (options.filesError) throw new Error('private-marker');
      return { data: options.files || [file] };
    } },
    git: { getBlob: async () => { calls.push('blob'); return { data: options.blob || blob }; } },
    issues: { listComments: () => {},
      createComment: async args => { calls.push(['create', args]); if (options.writeError) throw new Error('private-marker'); },
      updateComment: async args => { calls.push(['update', args]); } }
  } };
  github.paginate = async (method, args) => {
    if (method === github.rest.issues.listComments) {
      assert.deepEqual(args, { ...context.repo, issue_number: 7, per_page: 100 });
      calls.push('comments');
      return (options.pages || [[]]).flat();
    }
    throw new Error('只对评论使用自动分页');
  };
  const core = { setOutput: (key, value) => { outputs[key] = value; }, info: () => {},
    setFailed: message => errors.push(message) };
  return { github, context, core, env, calls, outputs, errors };
}

test('读取完整 PR 和固定版本正文，不采用外部 raw_url 或伪造正文', async t => {
  const h = await setup(t, { files: [{ ...file, raw_url: 'https://invalid.example', headContent: '伪造' }] });
  await readPR(h);
  assert.equal(h.outputs.ready, 'true');
  const input = JSON.parse(await fs.readFile(h.env.PR_INPUT));
  assert.equal(input.files[0].headContent, raw.toString());
  assert.equal(input.files[0].raw_url, undefined);
  assert.deepEqual(input.source, source);
  assert.equal(input.title, snapshot.title);
  assert.equal(input.body, snapshot.body);
  assert.deepEqual(h.errors, []);
  assert.deepEqual(h.calls, ['get', 'files', 'blob']);
});

test('标题未命中时不读取文件或正文，命中只继续读取', async t => {
  for (const [title, ready] of [
    ['feat: add authentication', false], ['fix: repair login', false], ['docs: add a new tutorial', false],
    ['', false], ['prefix: change doctor', false], ['DOCS: FIX README', true],
    ['修复文档链接', true], ['修正部分拼写错误', true], ['Fix typo', true], ['docs: correct the example', true]
  ]) {
    const h = await setup(t, { snapshots: [{ ...snapshot, title, body: null }] });
    await readPR(h);
    assert.equal(h.outputs.ready, String(ready), title);
    assert.deepEqual(h.calls, ready ? ['get', 'files', 'blob'] : ['get']);
    if (ready) assert.equal(JSON.parse(await fs.readFile(h.env.PR_INPUT)).body, '');
  }
});

test('混合代码、非文档和新增删除重命名不读取正文', async t => {
  for (const files of [[{ ...file, filename: 'main.py' }],
    ...['added', 'removed', 'renamed'].map(status => [{ ...file, status }]),
    [file, { ...file, filename: 'image.png' }]]) {
    const h = await setup(t, { files, snapshots: [{ ...snapshot, changed_files: files.length }] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'false');
    assert.ok(!h.calls.includes('blob'));
    assert.deepEqual(h.errors, []);
  }
});

test('保留工具指令、生成文档及支持的 txt 文档供 Python 判断', async t => {
  for (const filename of ['AGENTS.md', 'docs/generated/api.md', 'docs/guide.txt', 'README.txt', 'nested/CONTRIBUTING.txt']) {
    const h = await setup(t, { files: [{ ...file, filename }] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'true');
  }
});

test('空列表或文件超限时不调用模型', async t => {
  for (const files of [[], Array(21).fill(file), Array(100).fill(file)]) {
    const h = await setup(t, { files });
    await readPR(h);
    assert.equal(h.outputs.ready, 'false');
    assert.deepEqual(h.calls, ['get', 'files']);
    assert.deepEqual(h.errors, []);
  }
});

test('无效文本和接口错误停止流程且不输出原始错误', async t => {
  for (const options of [{ filesError: true },
    { blob: { ...blob, content: Buffer.from([255]).toString('base64') } }]) {
    const h = await setup(t, options);
    await readPR(h);
    assert.equal(h.outputs.ready, 'false');
    assert.equal(h.errors.length, 1);
    assert.ok(!h.errors.join('').includes('private-marker'));
  }
  const large = await setup(t, { blob: { ...blob, size: 200_001 } });
  await readPR(large);
  assert.equal(large.outputs.ready, 'false');
  assert.deepEqual(large.errors, []);
});

test('首次创建评论并原样传递中文及特殊字符', async t => {
  const h = await setup(t);
  const body = marker + '\n中文 ${expression} `command` "引号"';
  await fs.writeFile(h.env.COMMENT_PATH, body);
  await publishComment(h);
  const writes = h.calls.filter(Array.isArray);
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], ['create', { owner: 'sample', repo: 'project', issue_number: 7, body }]);
});

test('分页定位自己的评论并更新，重复结果不再写入', async t => {
  const old = { id: 123, user: { type: 'Bot', login: 'github-actions[bot]' }, body: marker + '旧结果' };
  const h = await setup(t, { pages: [...Array(11).fill(Array(100).fill({ body: '普通评论' })), [old]] });
  await publishComment(h);
  assert.equal(h.calls.find(Array.isArray)[0], 'update');
  assert.equal(h.calls.find(Array.isArray)[1].comment_id, 123);
  old.body = await fs.readFile(h.env.COMMENT_PATH, 'utf8');
  h.calls.length = 0;
  await publishComment(h);
  assert.ok(!h.calls.some(Array.isArray));
});

test('不覆盖用户或其他机器人伪造的标记评论', async t => {
  const h = await setup(t, { pages: [[
    { id: 1, body: marker, user: { type: 'User', login: 'alice' } },
    { id: 2, body: marker, user: { type: 'Bot', login: 'other[bot]' } }
  ]] });
  await publishComment(h);
  assert.equal(h.calls.find(Array.isArray)[0], 'create');
});

test('发布前检查标题描述版本和关闭状态，变化时不写入', async t => {
  for (const changed of [{ ...snapshot, head: { sha: 'c'.repeat(40) } },
    { ...snapshot, base: { sha: 'c'.repeat(40) } }, { ...snapshot, state: 'closed' },
    { ...snapshot, title: 'feat: new tutorial' }, { ...snapshot, body: 'Changed purpose' }]) {
    const h = await setup(t, { snapshots: [changed] });
    await publishComment(h);
    assert.deepEqual(h.calls, ['comments', 'get']);
  }
});

test('发布失败不重试且不输出接口中的私密文本', async t => {
  const h = await setup(t, { writeError: true });
  await publishComment(h);
  assert.equal(h.calls.filter(Array.isArray).length, 1);
  assert.equal(h.errors.length, 1);
  assert.ok(!h.errors.join('').includes('private-marker'));
});

test('读取 PR 后真实 Python 入口未配置模型时仍发布说明', async t => {
  const h = await setup(t);
  await readPR(h);
  const output = path.join(path.dirname(h.env.PR_INPUT), 'outputs');
  const env = { ...process.env, 'INPUT_FILES-JSON': h.env.PR_INPUT, COMMENT_PATH: h.env.COMMENT_PATH,
    GITHUB_OUTPUT: output };
  delete env['INPUT_MODEL-CONFIG'];
  delete env.LLM_MODEL;
  const completed = spawnSync(process.env.TEST_PYTHON || 'python', ['-I', path.join(__dirname, '../pr_checker.py')],
    { env, encoding: 'utf8' });
  assert.equal(completed.status, 0, completed.stderr);
  await assert.rejects(fs.access(output), { code: 'ENOENT' });
  await publishComment(h);
  const body = h.calls.find(Array.isArray)[1].body;
  assert.ok(body.includes('尚未配置模型'));
  assert.ok(body.includes('未进行修复审阅'));
});

test('所有读取阶段的跳过或失败都可发布明确原因', async t => {
  const scenarios = [
    [{ title: 'feat: login' }, 'title_not_matched'],
    [{ files: [] }, 'no_changes'],
    [{ files: Array(21).fill(file) }, 'scope_limit'],
    [{ files: [{ ...file, filename: 'main.py' }] }, 'outside_documentation_scope'],
    [{ files: [{ ...file, status: 'added' }] }, 'unsupported_file_status'],
    [{ blob: { ...blob, size: 200001 } }, 'invalid_document_content'],
    [{ filesError: true }, 'pr_read_failed'],
    [{ blob: { ...blob, content: Buffer.from([255]).toString('base64') } }, 'pr_read_failed']
  ];
  const messages = require('../templates/messages.json');
  for (const [options, reason] of scenarios) {
    const current = { ...snapshot, title: options.title || snapshot.title };
    const h = await setup(t, { ...options, snapshots: [current, current] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'false');
    assert.equal(h.outputs.reason, reason);
    h.env.PR_READ_REASON = h.outputs.reason;
    h.env.CHECK_OUTCOME = 'skipped';
    await publishComment(h);
    const body = h.calls.find(Array.isArray)[1].body;
    assert.ok(body.includes(messages.reasons[reason]));
    assert.ok(body.includes(source.headSha));
    assert.ok(body.includes('未进行修复审阅'));
    assert.ok(!body.includes('**建议合并**'));
  }
});

test('判断步骤失败或正文丢失时回复失败说明，不误发旧正文', async t => {
  for (const outcome of ['failure', 'skipped', 'success']) {
    const h = await setup(t);
    h.env.CHECK_OUTCOME = outcome;
    if (outcome === 'success') await fs.unlink(h.env.COMMENT_PATH);
    await publishComment(h);
    const body = h.calls.find(Array.isArray)[1].body;
    assert.ok(body.includes('判断步骤未完成或未生成评论'));
    assert.ok(!body.includes(marker + '\n建议合并\n'));
  }
});

test('手动和自动触发均读取 API 当前版本，不使用事件旧数据或工作流 SHA', async t => {
  const current = { ...snapshot, title: 'docs: correct spelling', body: 'Current description',
    base: { sha: 'd'.repeat(40) }, head: { sha: 'e'.repeat(40) } };
  for (const manual of [false, true]) {
    const h = await setup(t, { manual, snapshots: [current, current] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'true');
    assert.equal(h.outputs.resolved, 'true');
    const input = JSON.parse(await fs.readFile(h.env.PR_INPUT));
    assert.deepEqual(input.source, { baseSha: current.base.sha, headSha: current.head.sha, provider: 'github' });
    assert.equal(input.title, current.title);
    assert.equal(input.body, current.body);
    assert.equal(input.files[0].headContent, raw.toString());
    assert.equal(input.files[0].patch, file.patch);
    h.env.CHECK_OUTCOME = 'failure';
    await publishComment(h);
    const body = h.calls.find(Array.isArray)[1].body;
    assert.ok(body.includes(current.head.sha));
    assert.ok(body.includes(current.base.sha));
    assert.ok(!body.includes(h.context.sha || source.headSha));
    assert.deepEqual(h.errors, []);
  }
});

test('旧 PR 手动重审与自动流程共用分类、正确性校验和评论更新', async t => {
  const bodies = [];
  for (const manual of [false, true]) {
    const old = { id: 123, user: { login: 'github-actions[bot]' }, body: marker + '\n旧审阅结果' };
    const h = await setup(t, { manual, pages: [[old]] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'true');
    // 执行真实 Python 判断入口，只模拟模型响应；断言分类和正确性审阅各调用一次。
    const script = `
import json, sys
from unittest.mock import Mock
sys.path.insert(0, sys.argv[1])
from test_assessment import reply
from pr_checker import run_check
invoke = Mock(wraps=reply())
result = run_check(env={'INPUT_FILES-JSON': sys.argv[2], 'COMMENT_PATH': sys.argv[3], 'LLM_MODEL': 'test', 'GITHUB_ACTIONS': 'true'}, invoke=invoke)
assert result['status'] == 'candidate', result
assert invoke.call_count == 2
assert json.loads(invoke.call_args_list[0].args[0][1]['content']) == {'title': 'docs: fix typo', 'body': 'Correct a spelling mistake in the guide.'}
assert 'files' in json.loads(invoke.call_args_list[1].args[0][1]['content'])
`;
    const completed = spawnSync(process.env.TEST_PYTHON || 'python',
      ['-I', '-c', script, __dirname, h.env.PR_INPUT, h.env.COMMENT_PATH], { encoding: 'utf8' });
    assert.equal(completed.status, 0, completed.stderr);
    await publishComment(h);
    const writes = h.calls.filter(Array.isArray);
    assert.equal(writes.length, 1);
    assert.equal(writes[0][0], 'update');
    assert.equal(writes[0][1].comment_id, old.id);
    bodies.push(writes[0][1].body);
    assert.ok(bodies.at(-1).includes('**建议合并**'));
    assert.ok(bodies.at(-1).includes(source.headSha));
    old.body = bodies.at(-1);
    h.calls.length = 0;
    await publishComment(h);
    assert.ok(!h.calls.some(Array.isArray));
    assert.deepEqual(h.errors, []);
  }
  assert.equal(bodies[0], bodies[1]);
});

test('手动编号无效或 PR 不存在及无权读取时失败且不进入审阅或发布', async t => {
  for (const value of ['', undefined, '0', '-1', '1.5', 'abc', '9007199254740992']) {
    const h = await setup(t, { manual: true });
    h.context.payload.inputs.pr_number = value;
    await readPR(h);
    assert.equal(h.outputs.ready, 'false');
    assert.equal(h.outputs.resolved, 'false');
    assert.deepEqual(h.errors, ['PR 编号必须为正整数。']);
    assert.deepEqual(h.calls, []);
  }
  for (const manual of [false, true]) {
    for (const getError of [404, 403, 500]) {
      const h = await setup(t, { manual, getError });
      await fs.unlink(h.env.PR_SNAPSHOT);
      await fs.unlink(h.env.PR_INPUT);
      await readPR(h);
      assert.equal(h.outputs.ready, 'false');
      assert.equal(h.outputs.resolved, 'false');
      assert.equal(h.errors.length, 1);
      assert.match(h.errors[0], /无法读取 PR #7/);
      assert.ok(!h.errors[0].includes('private-marker'));
      assert.deepEqual(h.calls, ['get']);
      await assert.rejects(fs.access(h.env.PR_SNAPSHOT), { code: 'ENOENT' });
      await assert.rejects(fs.access(h.env.PR_INPUT), { code: 'ENOENT' });
    }
  }
});

test('手动重审期间标题描述提交或打开状态变化时不覆盖评论', async t => {
  for (const changed of [{ ...snapshot, head: { sha: 'd'.repeat(40) } },
    { ...snapshot, base: { sha: 'd'.repeat(40) } }, { ...snapshot, state: 'closed' },
    { ...snapshot, title: 'docs: add guide' }, { ...snapshot, body: 'New description' }]) {
    const h = await setup(t, { manual: true, snapshots: [snapshot, changed] });
    await readPR(h);
    assert.equal(h.outputs.ready, 'true');
    await publishComment(h);
    assert.ok(!h.calls.some(Array.isArray));
    assert.deepEqual(h.errors, []);
  }
});
