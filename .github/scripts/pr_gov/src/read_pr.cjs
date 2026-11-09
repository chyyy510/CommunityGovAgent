// 由 github-script 调用：读取 PR 文档改动，生成 Python 判断所需的临时输入。
module.exports = async ({ github, context, core, env = process.env }) => {
  // github、context、core 由官方 Action 提供；本地测试可以传入自己的 env。
  const fs = require('node:fs/promises');
  const number = Number(context.eventName === 'workflow_dispatch'
    ? context.payload.inputs?.pr_number : context.issue.number);
  const args = { ...context.repo, pull_number: number };

  // 默认跳过后续判断，只有完整写出输入后才将 ready 设为 true。
  core.setOutput('ready', 'false');
  core.setOutput('resolved', 'false');
  const skip = reason => core.setOutput('reason', reason);

  if (!Number.isSafeInteger(number) || number <= 0) {
    core.setFailed('PR 编号必须为正整数。');
    return;
  }

  let event;
  try {
    const { data: pr } = await github.rest.pulls.get(args);
    event = { number, title: pr.title, body: pr.body,
      base: { sha: pr.base.sha }, head: { sha: pr.head.sha } };
    // 两种触发方式共用 API 快照；即使后续跳过或失败，发布前也能核对本次读取的版本。
    await fs.writeFile(env.PR_SNAPSHOT, JSON.stringify(event), 'utf8');
    core.setOutput('resolved', 'true');
  } catch {
    core.setFailed(`无法读取 PR #${number}，请确认编号存在且工作流有读取权限，并检查接口状态。`);
    return;
  }

  try {
    // 先筛标题，未命中时连文件列表也不读取；命中只代表需要进一步分类。
    const rules = require('./title_rules.json');
    const title = event.title || '';
    const matches = name => new RegExp(rules[name], 'i').test(title);
    if (!(matches('languageRepair') || (matches('documentation') && matches('repair')))) return skip('title_not_matched');

    // 本工作流只处理最多 20 个文件的 PR；首批最多取 100 个，超过 20 个就跳过，无需继续翻页。
    const { data: files } = await github.rest.pulls.listFiles({ ...args, per_page: 100 });
    if (!files.length) return skip('no_changes');
    if (files.length > 20) return skip('scope_limit');

    // 这里只筛文件范围；是否属于修复、修复是否正确仍由 Python 调用 LLM 判断。
    const document = name => /\.(md|markdown|rst)$/i.test(name)
      || (/\.txt$/i.test(name) && (/(^|\/)(readme|contributing)\.txt$/i.test(name)
        || /^(doc|docs|documentation)\//i.test(name)));
    // 混合代码修改、整文件新增删除和重命名均跳过整个 PR。
    if (files.some(file => !document(file.filename))) return skip('outside_documentation_scope');
    if (files.some(file => file.status !== 'modified')) return skip('unsupported_file_status');

    const input = {
      schemaVersion: 1, complete: true, title, body: event.body || '',
      source: { baseSha: event.base.sha, headSha: event.head.sha, provider: 'github' }, files: []
    };
    for (const { filename, status, additions, deletions, patch, sha } of files) {
      // 按文件列表返回的 SHA 读取该版本的完整内容，不使用 PR 中提供的链接。
      const { data: blob } = await github.rest.git.getBlob({ ...context.repo, file_sha: sha });
      if (blob.size > 200_000) return skip('invalid_document_content');

      const headContent = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })
        .decode(Buffer.from(blob.content, 'base64'));
      input.files.push({ filename, status, additions, deletions, patch, headContent });
    }

    // 仅传递临时输入；读取后不再查询 PR 状态，版本检查留在发布评论前。
    await fs.writeFile(env.PR_INPUT, JSON.stringify(input), 'utf8');
    core.setOutput('ready', 'true');
  } catch {
    skip('pr_read_failed');
    core.setFailed('无法完整读取 PR，请检查权限、接口状态及文件内容。');
  }
};
