// 由 github-script 调用：检查 PR 版本后创建或更新本检查的机器人评论。
module.exports = async ({ github, context, core, env = process.env }) => {
  const fs = require('node:fs/promises');

  // 与 comments.py 生成的正文标记一致，用于定位这项检查的旧评论。
  const marker = '<!-- truecontribution:documentation-repair-review:v1 -->';

  try {
    const event = JSON.parse(await fs.readFile(env.PR_SNAPSHOT, 'utf8'));
    let body;
    if (env.CHECK_OUTCOME === 'success') {
      try {
        body = await fs.readFile(env.COMMENT_PATH, 'utf8');
      } catch {
        // 判断步骤没有生成正文时，下面改用检查失败说明。
      }
    }

    // 标题筛选、文件读取或 Python 步骤未完成时，也生成一条结果说明。
    if (!body) {
      const path = require('node:path');
      const messages = require('../templates/messages.json');
      const code = env.PR_READ_REASON || 'check_failed';
      const values = {
        marker, status: messages.comment.not_reviewed,
        reason: messages.reasons[code] || messages.reasons.check_failed,
        head_sha: event.head.sha, base_sha: event.base.sha
      };
      const template = await fs.readFile(path.join(__dirname, '../templates/pr_status_comment.md'), 'utf8');
      body = template.replace(/\$(marker|status|reason|head_sha|base_sha)\b/g, (_, key) => values[key]);
    }

    const args = { ...context.repo, issue_number: event.number };

    // PR 下的普通评论使用 issues 接口；官方分页方法会读取后续页。
    const comments = await github.paginate(github.rest.issues.listComments, { ...args, per_page: 100 });
    // 同时匹配机器人账号和标记，避免覆盖用户或其他机器人的评论。
    const existing = comments.find(comment => comment.user?.login === 'github-actions[bot]'
      && comment.body?.startsWith(marker));

    // 等待模型期间 PR 可能有更新；发布前确认标题、描述和两侧提交都没变，且 PR 仍打开。
    const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: event.number });
    if (pr.state !== 'open' || pr.title !== event.title || (pr.body || '') !== (event.body || '')
      || pr.base.sha !== event.base.sha || pr.head.sha !== event.head.sha) return;

    // 重跑得到相同正文时不写入；有旧评论则更新，没有才创建。
    if (existing?.body === body) return;
    if (existing) {
      await github.rest.issues.updateComment({ ...context.repo, comment_id: existing.id, body });
    } else {
      await github.rest.issues.createComment({ ...args, body });
    }
  } catch {
    // 请求报错时，评论可能已经发出；不自动重试，以免重复评论，也不打印接口的原始错误信息。
    core.setFailed('无法发布 PR 审阅建议，请检查令牌权限及接口状态。');
  }
};
