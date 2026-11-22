# 离线评测报告发布目录

API 从只读挂载目录 `/data/evaluation` 加载 `reports.json`，文件缺失时显示空状态。可以用 `SF_EVALUATION_REPORTS_DIR` 指定挂载目录。演示启动脚本优先使用显式配置和此目录内已有的本地 `reports.json`，两者都缺失时挂载 `demo` 子目录。

随仓库发布的[演示报告](demo/reports.json)供演示租户 `tenant_a` 的客服、主管和管理员读取。两份报告对应 38 个检索单元与 39 个可靠性单元。[结果与证据](../../../docs/verification/sf-ri.md)

离线实验完成后，将已脱敏的报告摘要整理为 `{"schema_version": 1, "reports": [...]}`。每条报告由发布方指定 `tenant_id` 和 `allowed_roles`。列表和详情按会话中的当前租户、角色过滤。文件支持 200 条以内报告，总大小上限为 512 KiB。

报告包含 `report_id`、`title`、`experiment_id`、带时区的 `generated_at` 和 `scope`。`baseline` 可填实验臂、模型、图版本和源码版本，未发布则为 `null`。`metrics` 填真实分子和分母，待实测项两者填 `null`。`failure_classes` 填失败分类和次数。`experiment_url` 可填发布后的 HTTPS 实验链接或 `null`。文件字段与合法值见接口模型。

部署者可通过临时文件和原子替换发布本地 `reports.json`。此目录内的本地报告文件由 Git 忽略，`demo` 子目录中随仓库发布的是已脱敏的固定摘要。正式原始证据保存在对应实验归档。[评测接口模型](../../../apps/supportflow/api/evaluation.py)
