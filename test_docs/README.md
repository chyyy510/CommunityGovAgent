# GitHub PR 模拟样例

这里保存 cann-learning-hub 七个真实 PR 修改前的文件。每个 `pr-编号/` 子目录保留上游相对路径；对应测试分支只提交该 PR 的原始修改，不修改工作流或其他样例。

来源为已有的 [cases.json](../.github/scripts/pr_gov/tests/real_pr/cann-learning-hub/cases.json) 和 [sources.json](../.github/scripts/pr_gov/tests/real_pr/cann-learning-hub/sources.json)。#528 的图片按记录中的固定提交补取，并核对了 SHA-256 和 Git blob SHA。上游内容及版权声明保持原样，许可证见 [UPSTREAM-LICENSE.txt](UPSTREAM-LICENSE.txt)。

这些是测试材料，不需要运行其中的 Shell 脚本或 Notebook。只复制 PR 涉及的文件，因此文档中引用的其他图片和相对链接可能无法打开。`.gitattributes` 保留文件的原始字节和换行，确保 Shell 换行修复不会被 Git 自动转换掩盖。

## 分支和测试目的

所有分支从同一份 `main` 基线创建，创建 PR 时目标分支都选 `main`，不要将测试分支互相合并。首次测试前，先把含本目录和工作流的 `main` 推送到 GitHub，再推送测试分支。

| 分支 | 上游 PR | 修改内容 | 观察重点 |
| --- | --- | --- | --- |
| `test/pr-1407-text-fixes` | [#1407](https://gitcode.com/cann/cann-learning-hub/pull/1407) | 重复词、缺字和量词修复 | 简单文档修复的正常审阅流程 |
| `test/pr-1242-unit-format` | [#1242](https://gitcode.com/cann/cann-learning-hub/pull/1242) | 将数值单位的 LaTeX 写法改为普通文本 | 保留技术含义的排版修改 |
| `test/pr-1405-link-target` | [#1405](https://gitcode.com/cann/cann-learning-hub/pull/1405) | 修正 PyTorch 文档链接目标 | 新提示词是否将需要核实链接目标的修复排除 |
| `test/pr-208-command-name` | [#208](https://gitcode.com/cann/cann-learning-hub/pull/208) | 修正 Notebook 命令名及“准入”措辞 | 描述明确包含技术修复时的分类 |
| `test/pr-528-mixed-files` | [#528](https://gitcode.com/cann/cann-learning-hub/pull/528) | Markdown、Notebook 修改及图片重命名 | 文件范围检查跳过，不调用模型，评论说明原因 |
| `test/pr-1135-shell-newlines` | [#1135](https://gitcode.com/cann/cann-learning-hub/pull/1135) | 四个 Shell 文件从 CRLF 改为 LF | 标题关键词未命中时直接跳过 |
| `test/pr-1380-heading-levels` | [#1380](https://gitcode.com/cann/cann-learning-hub/pull/1380) | 五篇博客共 34 处标题层级调整 | 大改动的请求大小限制；分类通过后仍可能因 45,000 字节上限无法完成审阅 |

模型结论以实际运行结果为准。以上是测试目的，不是已经验证的线上结果。

## 建议使用的 PR 标题和描述

标题用于关键词初筛；标题和描述共同用于模型分类。以下描述说明实际改动，不包含对模型输出的指令。

### #1407

标题：`docs: 修正课程说明中的重复词、缺字和量词`

描述：修正“数据表示表示”为“数据表示”，补齐“硬件功能部件”中的缺字，并将“二种模式”改为“两种模式”。仿照 cann-learning-hub #1407，用于本仓库的 PR 流程测试。

### #1242

标题：`docs: 修正数值单位的排版`

描述：将两处数值单位的 LaTeX 写法改为普通文本，保留原有数值和单位。仿照 cann-learning-hub #1242。上游标题提到了重复标点，这里按实际 diff 描述修改。

### #1405

标题：`docs: 修正 PyTorch 文档链接目标`

描述：将 PyTorch 2.0 文档链接从 pytorch.org 调整到 docs.pytorch.org，并补充末尾斜杠。该修改涉及链接目标是否正确，仿照 cann-learning-hub #1405。

### #208

标题：`docs: 修正 Notebook 命令名及准入标题`

描述：将代码单元写文件命令从 %%write 改为 %%writefile，并将两处“准合入”改为“准入”。该修改同时涉及命令含义和标题措辞，仿照 cann-learning-hub #208。

### #528

标题：`docs: 修正文档拼写和图片引用`

描述：修正 CONTRIBUTION.md 和 Notebook 中的拼写与图片引用，并将 traning_inference.png 重命名为 training_inference.png。仿照 cann-learning-hub #528，包含三种文件修改。

### #1135

标题：`fix(sh): 将四个脚本的 CRLF 换行修正为 LF`

描述：保留四个 Shell 脚本的文本内容，将 CRLF 换行改为 LF，以修复 bad interpreter 问题。仿照 cann-learning-hub #1135。

### #1380

标题：`docs: 修复博客文档标题层级跳级`

描述：调整五篇博客中的标题层级，修正从一级标题直接跳到三级标题等结构问题，保持正文内容不变。仿照 cann-learning-hub #1380。

## 推送命令

```sh
git push origin main
git push origin test/pr-1407-text-fixes test/pr-1242-unit-format test/pr-1405-link-target test/pr-208-command-name test/pr-528-mixed-files test/pr-1135-shell-newlines test/pr-1380-heading-levels
```

创建 PR 后可编辑标题或描述，或向对应分支提交修改，检查工作流是否再次触发。所有结果都会回复：关键词未命中、分类为范围外或分类不确定时，也会说明原因。若此前已有机器人评论，会更新同一条评论；其提交版本可用于区分新旧结果。
