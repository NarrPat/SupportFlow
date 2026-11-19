# 普通部署演示数据

在 Windows 项目根运行下列命令。原有 FX-BASE 先在持久数据库中迁移和 seed，随后单次插入邀请事实。现有知识 setup 发布已随产品交付的临时席位政策，沿用当前租户 ACL 和索引代。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/serve-workspace.ps1 -Minutes 15 -WithWorker -WithKnowledge -WithProposal -WithApproval -WithExecution -WithDemoData
```

用 `a_support` 登录，在工单页选择“新建工单”并关联青篱设计 C-001。回到工作台选择该工单，创建关联会话并提交明确的临时席位申请。申请邮箱用 `member@example.com`，例如 20 席位和 7 天。另一个浏览器会话使用 `a_supervisor` 打开审批页，核对提案后决定。回到原运行，核验回执与订阅版本。租户 B 的 `b_support` 可按同样的工单与会话步骤读取 `analyst@example.net` 的邀请及仍有余量的订阅。

仅演示只读核查时可使用 `-WithWorker -WithDemoData`，增加 `-WithKnowledge` 后可查看知识引用。Worker profile 使用该项目本地配置的模型凭据。启动和 seed 不提交业务运行。

演示事实使用保留域名和合成客户。邀请有效期为创建后 30 天，以首次 seed 的 PostgreSQL 事务时间确定创建和更新日期。再次启动通过 fixture 字节 SHA-256 识别已应用 seed，沿用原始日期。若目标键已有其他事实，整个插入事务回滚。已提交回执、订阅、已有租户与 ACL 均保留。已授予或已过期时，页面依据当前规则返回实际状态。重复演示正向授予使用独立验收环境。
