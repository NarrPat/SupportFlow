# SupportFlow 原计划检索与可靠性评测

实验 `sf-ri-original-plan-v1` 共核验 77 个单元，其中检索 38 个、可靠性 39 个。数据为合成 SaaS 场景，检索查询和可靠性场景均为冻结的已见样本。[77 个单元的脱敏账本](sf-ri-results.json) · [工作台发布目录](../../deploy/supportflow/published-reports/README.md)

## 检索

19 道查询分别以纯向量 `R0` 和向量＋BM25＋RRF 的 `R1` 执行，使用同一份语料、权限和本地 BGE 嵌入模型。B14202（`69f72207`）有 8 个复用单元。B14862（`76cdbfb6`）补齐 30 个登记单元，也对 8 个历史单元重新采集以校验可比性。B14863 在新库重建索引，38 个单元的候选顺序与引用全部一致。

| 策略 | Hit@5 | 找齐全部必要证据 |
| --- | ---: | ---: |
| R0 纯向量 | 15/19 | 7/19 |
| R1 向量＋BM25＋RRF | 18/19 | 6/19 |

15 题两种策略都命中，3 题仅混合检索命中，`SF-025` 两种策略均未命中。两次独立权限探测均拒绝越权引用读取。Hit@5 衡量前五候选是否至少含一条合法 gold，找齐全部证据另列。检索批次未调用生成模型。[逐项命中判定与采集批次索引](sf-ri-results.json)

## 可靠性

13 个应提交一次的场景各 3 轮，复用原 v9 提交 `d4e82ee` 的 39 个 `H1-S0` 原运行。独立核对 PostgreSQL 的业务前后快照、回执、版本和最终状态。

| 指标 | 结果 |
| --- | ---: |
| 恰好一次业务提交 | 39/39 |
| 符合预期办结 | 39/39 |
| 发生重复业务变更的运行 | 0/39 |
| 漏执行 | 0/39 |
| 额外副作用 | 0 次 |

每轮新增一份回执，业务版本从 7 增至 8。36 轮成功，3 轮按预期在提交后取消。4 轮首次 AI 审核拒绝，追加绑定原运行的重审通过记录。原版 v9 的 534 单元总账独立保留。[逐项提交、终态、审核状态与来源索引](sf-ri-results.json)

## 原始证据

[脱敏账本](sf-ri-results.json)包含逐项 ID、判定、批次与原始文件 SHA-256。完整开发验收工作区中，以下路径均相对工程根目录。发布的工作台报告使用同一归档时间与统计口径。

| 原始材料 | 开发工作区路径 |
| --- | --- |
| 索引及保留的失败批次 | `artifacts/iterations/SF-I001/development-notes/sf-ri-evidence-index.json` |
| 检索逐项账本 | `artifacts/iterations/SF-I001/development-notes/sf-ri-retrieval-final.json` |
| 可靠性逐项账本 | `artifacts/iterations/SF-I001/development-notes/sf-ri-reliability-final.json` |
| 复现命令和批次说明 | `artifacts/iterations/SF-I001/development-notes/sf-ri-handoff.md` |

B14860、B14861 的采集登记错误、首次评分与重审原档继续按原批次留存。GitHub 运行快照随附脱敏索引，详细运行日志、查询内容、数据库快照和审核入参保存在本地验收工作区。
