# cann-learning-hub 真实 PR 测试

这组语料来自 [cann/cann-learning-hub](https://gitcode.com/cann/cann-learning-hub)，于 2026-11-09 通过公开 GitCode API 采集。保留 7 个 PR 的完整变更文件列表、文本补丁、固定版本正文和来源证明，用于测试当前文档修复初筛。

PR 标题、作者的测试描述和是否已合并都不作为修复正确的依据。样例预期由本项目根据实际 diff 和当前能力边界设定，不是上游维护者的独立标注，也不是随机抽样的准确率基准。

当前流程先匹配标题关键词，再仅根据标题和描述调用模型分类；确认是文档修复后，才进行第二次模型调用检查具体改动。旧样例只在 `sources.json` 保存了标题，未保存 PR 描述。`run.py` 运行时补入标题、将描述留空，不修改归档输入或编造描述；这组样例暂不能验证真实 PR 描述对分类的影响。以后重新采集时会同时保存标题和描述。

当前模型回复字段为 `type`、`assessment`，并由模型判断新增或删除段落、特殊文件及事实修复。下方实测结果及 `results/` 中记录来自旧版实现（当时标记为 `documentation-llm-v1`），保留原样供查阅。测试脚本不读取历史回复，使用 `--live` 时每次都重新请求模型。固定 PR 输入仍可用于新测试。

当前 Markdown 提示词进一步将候选范围收紧为简单文档修复，复杂技术修复或混合修改应在分类阶段返回 `other`。因此旧样例预期不能用来证明新提示词的分类效果；特别是链接、命令等修复是否进入审阅，取决于新的标题描述分类结果。

`cases.json` 中的预期也来自当时的规则，不代表当前实现已经达到的模型效果。当前标题规则会排除 #1135，#528 随后因文件范围被排除。#1380 的 34 处改动按批次完整审阅，不因整个 PR 的总输入超过 45,000 字节而跳过。#1405、#208 也不再因没有参考引用而必然无法获得合并建议。共享上下文版本已对 #1380 重新调用真实模型，结果见下节；其余样例尚未重新实测。

## 共享上下文的体积与契约验证

2026-11-09，以固定 #1380 输入比较优化前版本 `4b8ca2c` 和共享上下文版本 `7dec31f`。两版均为 7 批，分别含 5、5、5、5、5、5、4 个 change。以下统计 UTF-8 序列化 `messages` 的字节数，包含各版 system Prompt、标题和 user JSON 的转义，与 45,000 字节限制的口径一致；不含 HTTP 外层模型参数，不含分类请求或重试。

| 审阅批次 | 优化前字节数 | 优化后字节数 |
| --- | ---: | ---: |
| 1 | 10,076 | 8,151 |
| 2 | 12,055 | 8,443 |
| 3 | 11,588 | 8,153 |
| 4 | 12,326 | 8,311 |
| 5 | 10,935 | 9,057 |
| 6 | 12,168 | 8,979 |
| 7 | 7,777 | 7,151 |
| 合计 | 76,925 | 58,245 |

消息总量下降 18,680 字节（24.3%），最大单批从 12,326 降到 9,057 字节。新输入分别携带 base/head 上下文，保留完整 change 原文；此样例仅调整标题层级，因此主要通过共享标题片段并省去无关正文减少输入。收益取决于改动分布和上下文，不能据此断言所有 PR 都缩小。

获得本次外部处理授权后，使用已有测试入口的 `evaluate`、`model_environment` 对 #1380 单独调用 `deepseek-flash`，配置为 `thinking: disabled`、`maxOutputTokens: 8192`。分类第一次返回的结构不符合契约，重试后通过；7 个正确性审阅批次均首次通过校验，共 9 次请求。结果包含 `change-1` 至 `change-34`，每个 ID 恰好一次，34 项均为 `improved`，最终为 `candidate`。此次未向 GitHub 或 GitCode 写入内容，也未改写历史 `results/`、输入或哈希。

79 项离线回归通过，覆盖区间重叠与相接、跨文件、重复文本、base/head 行号偏移、标题片段、实际字节分批、超限单项、重试边界及结果汇总。`test_real_pr_samples.py` 校验真实快照还原的 base 行号，并对 #1380 检查请求体积下降及 34 个 ID 的完整性；`test_contexts.py` 还使用同一 Prompt 比较相邻长正文的独立上下文与共享上下文。实测验证了这一样例的输入与输出契约，不代表已验证其他文档的模型判断准确率。

## 样例与本次结果

真实运行使用 `deepseek-flash`、JSON 输出、`thinking: disabled`，共 4 次模型请求。3 次回复通过响应契约，1 次因缺少事实引用被拦截。没有向 GitCode 或 GitHub 写入评论、标签或审阅。

| PR | 实际改动 | 文件数 / 增删行数 | 本次结果 |
| --- | --- | --- | --- |
| [#1407](https://gitcode.com/cann/cann-learning-hub/pull/1407) | README 中的重复词、缺字和量词修正 | 1 / 6 | `candidate`，建议合并 |
| [#1242](https://gitcode.com/cann/cann-learning-hub/pull/1242) | 将数值单位的 LaTeX 表述改为普通文本；实际 diff 与标题的“重复标点”描述不同 | 1 / 4 | `candidate`，建议合并 |
| [#1405](https://gitcode.com/cann/cann-learning-hub/pull/1405) | PyTorch 文档链接目标修正 | 1 / 2 | 模型认为事实证据不足，暂不建议合并 |
| [#208](https://gitcode.com/cann/cann-learning-hub/pull/208) | `%%write` 改为 `%%writefile`，以及标题措辞修正 | 1 / 6 | 模型回复未满足事实引用要求，`factual_evidence_missing`，暂不建议合并 |
| [#528](https://gitcode.com/cann/cann-learning-hub/pull/528) | Markdown、Notebook 修改及图片重命名 | 3 / 6 | 超出文档范围，跳过，不调用模型 |
| [#1135](https://gitcode.com/cann/cann-learning-hub/pull/1135) | 4 个 shell 脚本的换行修复 | 4 / 144 | 代码修改且超出增删行数限制，跳过，不调用模型 |
| [#1380](https://gitcode.com/cann/cann-learning-hub/pull/1380) | 5 篇博客的标题层级修复，共 34 处 | 5 / 68 | 超过 20 处修改限制，跳过，不调用模型 |

本次 7 个分流结果与预期一致，离线回放也一致。`#1405` 和 `#208` 没有附加可信事实参考，结果反映现有证据限制，不能据此断言它们的改动错误。`#1380` 展示了当前范围限制会跳过较大的纯文档修复。

## 文件

- [cases.json](cases.json)：平台无关 `schemaVersion: 1` 输入与样例预期。每个文本文件额外保存 `baseContent` 供完整性验证；运行时只使用现有标准字段，不把整个 base 正文额外发送给模型。
- [sources.json](sources.json)：PR 链接、固定 head、比较基线、文件数量、正文 Git blob SHA/SHA-256、补丁摘要及快照复查结果。未保存作者资料或讨论区内容。
- [results/offline/report.json](results/offline/report.json)：未调用模型的初筛结果。两条语言修复返回 `model_not_configured`，因此其中的 `matches: 5` 不代表模型判断失败。
- [results/deepseek/report.json](results/deepseek/report.json)：真实 API 测试结果。只保存通过契约校验的模型回复；无效回复不留原文。
- [results/replay.json](results/replay.json)：旧版复用模型回复时生成的历史记录；该功能已删除，文件仅供查阅。
- `results/deepseek/gitcode-cann-learning-hub-*.md`：4 个符合范围 PR 的本地评论预览。3 个跳过案例没有评论文件。
- [collect.py](collect.py)、[run.py](run.py)：采集与测试入口，均不执行上游代码。
- [UPSTREAM-LICENSE.txt](UPSTREAM-LICENSE.txt)：从固定上游提交取得的许可证原文；上游内容沿用该许可证，不因作为测试语料而改变授权。

## 完整性检查

GitCode PR API 返回的 `base.sha` 是采集时目标分支的 tip。采集器从 PR 提交的外部父提交中寻找历史基线，要求未截断的 compare 结果与 PR 完整文件列表、增删计数和补丁一致；不能将当前目标分支当作历史 base。

对修改过的文本文件，按固定 SHA 读取 base/head 正文，验证 Git blob 哈希，并用补丁从 base 重建 head，要求逐字节一致。`#528` 的图片重命名在 compare 接口中标为 `modified`；采集器另外核对旧路径和新路径的 blob 完全相同，保留重命名元数据，不保存图片二进制。采集结束后重新核对 PR head 和完整文件列表。

这些检查证明固定输入忠于公开 PR 差异，不证明修改的语义正确。当前采集脚本仅用于本地测试，不是已上线的 AtomGit Action 适配器；本次也没有验证 GitHub Runner、fork 权限或线上评论发送。

## 复跑

以下命令在 `.github/scripts/pr_gov` 目录执行。输出目录必须不存在，避免覆盖已有观察记录。

```sh
# 离线：不读密钥，不访问网络
python -I tests/real_pr/cann-learning-hub/run.py --output-dir NEW_OFFLINE_DIRECTORY

# 真实 API：读取仓库根目录 .env 或进程环境中的 DEEPSEEK_API_KEY / VALUE_MODEL_API_KEY
# 可选 LLM_MODEL；默认 deepseek-flash。会产生模型调用费用，不写平台评论。
python -I tests/real_pr/cann-learning-hub/run.py --live --output-dir NEW_LIVE_DIRECTORY

# 包含这组真实语料的回归测试；不联网、不读取真实密钥
python -X utf8 -m unittest discover -s tests -v
```

根目录 `.env` 已被 Git 忽略。运行器只从该文件加载模型相关变量，不执行其中的文本，不打印或保存密钥。当前固定语料存在时，`collect.py` 拒绝覆盖；重新采样应放入新的明确目录并重新核对预期。

测试入口已合并到本目录的 `run.py`。已有实测记录保持原始内容，其中源码路径和哈希对应当时的实现；不会因为精简目录而重新生成。
