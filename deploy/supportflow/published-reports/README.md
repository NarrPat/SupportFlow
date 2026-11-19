# 离线评测报告发布目录

评测页面读取此目录的 `reports.json`。部署将目录只读挂载至 API 的 `/data/evaluation`。文件缺失时页面显示空状态。

离线实验完成后，将已脱敏的报告摘要整理为 `{"schema_version": 1, "reports": [...]}`。每条报告由发布方指定 `tenant_id` 和 `allowed_roles`。列表和详情按会话中的当前租户、角色过滤。文件支持 200 条以内报告，总大小上限为 512 KiB。

报告包含 `report_id`、`title`、`experiment_id`、带时区的 `generated_at` 和 `scope`。`baseline` 可填实验臂、模型、图版本和源码版本，未发布则为 `null`。`metrics` 填真实分子和分母，待实测项两者填 `null`。`failure_classes` 填失败分类和次数。`experiment_url` 可填发布后的 HTTPS 实验链接或 `null`。文件字段与合法值见接口模型。

由部署者通过临时文件和原子替换发布。目录忽略本地报告数据，正式证据保存在对应实验归档。 [评测接口模型](../../../apps/supportflow/api/evaluation.py)
