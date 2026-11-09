# gov-test

一个简单文档修复 PR 检查工作流：PR 新建或更新时，先用标题关键词筛选，再让 DeepSeek 仅根据标题和描述判断是否属于简单文档修复。确认后分批调用模型检查具体改动，并在 PR 下回复是否建议合并。所有结果都会回复；未命中关键词、超出范围或检查失败时，评论说明原因。

```text
.github/
├── workflows/
│   └── documentation-repair-screening.yml
└── scripts/
    └── pr_gov/
        ├── README.md
        ├── pr_checker.py          # Python 判断入口
        ├── src/                   # Python 判断实现及 PR 读取、评论发布脚本
        │   └── title_rules.json   # 标题关键词规则
        ├── prompts/               # 模型提示词
        ├── templates/             # PR 评论模板及说明文案
        ├── tests/                 # 必要回归测试和真实 PR 样例
        └── requirements-dev.txt   # 仅供运行测试
```

在仓库 **Settings → Secrets and variables → Actions** 添加 `DEEPSEEK_API_KEY` Secret。可选配置 `LLM_MODEL` Variable；当前默认 `deepseek-flash`。将工作流及脚本提交到默认分支和 PR 的目标分支后，新建或更新 PR 即可触发。

配置、运行范围和本地测试命令见[脚本说明](.github/scripts/pr_gov/README.md)。

旧 PR 可在 **Actions → 文档修复初筛建议 → Run workflow** 中选择包含最新版工作流和脚本的可信分支，填写 `pr_number` 后重审。工作流按 API 返回的 PR 当前版本重新分类和审阅，并更新已有机器人评论。默认分支需先包含 `workflow_dispatch` 配置，页面才会显示手动运行入口，见 [GitHub 手动触发说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)。已关闭或已合并的 PR 仍按原规则不发布评论。

工作流使用官方 `actions/github-script` 读取 PR 和维护评论，自动使用工作流令牌。Python 负责提取改动、调用 DeepSeek 和生成评论内容；步骤之间通过 runner 临时文件传递数据。

正确性审阅按文件共享 base/head 上下文，合并同一版本中重叠或相邻的行号区间。每批最多 5 项，并按实际序列化消息大小控制在 45,000 字节以内；每项修改的原文保持完整。

部署后只回复 PR 评论，不生成报告、运行摘要或附件。本地样例测试仍可输出 JSON 报告和评论预览。
