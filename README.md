# SupportFlow｜SaaS 工单受控写操作 Agent

当客户说「同事收到了邀请，为什么还是加不进来」，客服可以在 SupportFlow 工单中核对邀请状态、域名策略与剩余席位，并打开诊断引用，查看当时使用的政策版本和原文行号。这个工作台运行在模拟双租户 SaaS 数据上。

如果需要临时扩容，客服在当前工单中明确席位数和有效期，另一位主管在独立会话中审批。授予临时席位是系统唯一的业务写动作。工作台会显示执行进度与回执，客服可以追查谁批准了哪些参数、订阅版本如何变化，以及响应丢失后实际是否已经生效。

| 从一张工单看实现 | 代码入口 |
| --- | --- |
| React 工作台提交请求，FastAPI 处理会话和权限。运行进度通过 SSE 推送，断线后按事件游标续读。 | [工作台](apps/supportflow/web/product) · [API](apps/supportflow/api) · [事件客户端](apps/supportflow/web/product/event_client.ts) |
| Python / LangGraph 编排诊断，从业务工具读取事实，在 PostgreSQL / pgvector 中检索有权读取的知识。提案由代码核验政策和参数。 | [Agent](apps/supportflow/agent) · [检索](apps/supportflow/retrieval/service.py) · [提案规则](apps/supportflow/domain/grant_policy.py) |
| 主管批准后，Celery worker 在写入前重新核对权限与业务版本。可选的 Spring Boot 服务在自己的 PostgreSQL 事务中更新订阅并写入回执。若写请求的响应丢失，Python 按原动作查询回执并核对订阅。 | [派发](apps/supportflow/jobs/approval_dispatch.py) · [Java 业务服务](apps/supportflow/spring-sandbox) · [对账](apps/supportflow/jobs/grant_reconciliation.py) |

## 快速启动

准备 Windows x64、PowerShell、Git、已有 pip 的引导 Python、Node 24.14.0、npm 11.9.0，以及正在运行的 Docker Desktop（Linux 容器）。首次安装和下载嵌入模型需要联网。从仓库根执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup-env.ps1 -Stage Install
New-Item -ItemType Directory -Force .local | Out-Null
.venv/Scripts/python.exe -I -B tools/prepare_embedding.py --output .local/embedding-setup.json
```

安装脚本使用锁定的 Python 3.12.14、uv 0.13.7 和前后端依赖。要运行完整 Agent 演示，请用本机文本编辑器把**自己的 DeepSeek API 密钥**保存到 `deploy/supportflow/.secrets/deepseek_api_key`，作为单行文本。该目录被 Git 忽略。安装脚本会生成数据库和服务凭据；运行脚本会生成演示账号的密码。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/serve-workspace.ps1 -Minutes 15 -WithWorker -WithKnowledge -WithProposal -WithApproval -WithExecution -WithDemoData
```

打开 [本机工单工作台](http://127.0.0.1:18080/tickets)。客服 `a_support`、主管 `a_supervisor` 的密码见本机 `deploy/supportflow/.secrets/seed-accounts.json`。用两个独立浏览器会话登录。演示结束后容器和网络会自动清理，数据卷保留。只验证登录和工单界面时，可只传 `-Minutes 5` 启动；这样不需要模型密钥，不会启动 Agent，仍需要 Docker 和已安装依赖。

## 演示与验证

- **诊断与引用**：为 C-001 建立工单和会话，提交「请核查 C-001 的 member@example.com 无法加入，检查邀请、席位和域名并给出依据」。打开引用核对正文、版本及行号。[诊断路由](apps/supportflow/domain/diagnosis_routing.py) · [检索及引用授权](apps/supportflow/retrieval/service.py)
- **受控写入**：在 C-001 工单明确申请 `member@example.com` 的 10 个临时席位、3 天。用独立主管会话审批，回原运行核对唯一回执。如当前邀请、容量或冷却状态已变，系统可能不给出可审批的提案。[可信政策](apps/supportflow/domain/grant_policy.py) · [执行前授权](apps/supportflow/jobs/approval_dispatch.py)
- **故障恢复**：在同一个运行页切换浏览器网络，观察 SSE 游标续传。业务响应丢失的处理见[回执核对](apps/supportflow/jobs/grant_reconciliation.py)，客户端重连见[事件客户端](apps/supportflow/web/product/event_client.ts)。普通演示未主动注入沙箱响应故障。

仅验证前端的构建和客户端逻辑：

```powershell
npm.cmd --prefix apps/supportflow/web ci --no-fund --no-audit
npm.cmd --prefix apps/supportflow/web run build:product
$ui = @(Get-ChildItem tests/supportflow -Filter 'ui_*.test.mjs' | Sort-Object Name | ForEach-Object FullName)
node --test @ui
```

## 评测结果

在工作台侧边栏打开「评测」，可查看原计划检索和可靠性报告。启动本机演示后可直接打开[评测页](http://127.0.0.1:18080/evaluation)。[逐项结果与证据](docs/verification/sf-ri.md)

| 原计划评测 | 结果 |
| --- | --- |
| 检索 38/38 | 19 道查询各运行两种策略。纯向量与混合检索 Hit@5 分别为 **15/19、18/19**，找齐全部必要证据分别为 **7/19、6/19**。复用 8 个单元，补跑 30 个，重建索引后全部复核一致。[检索证据](docs/verification/sf-ri.md#检索) |
| 可靠性 39/39 | 13 个场景各 3 轮，**39/39 恰好一次提交并符合预期办结**，重复变更 0/39、漏执行 0/39、额外副作用 0 次。[可靠性证据](docs/verification/sf-ri.md#可靠性) |

## Spring Boot 业务服务

在完整演示命令末尾加 `-WithJavaSandbox`，并准备支持 Java 21 字节码构建的 JDK（本次使用 JDK 25）。脚本用锁定的 Maven 构建 Java 模块。Java 使用独立 PostgreSQL 业务卷；Python 沿固定内部地址读取事实、批准后派发，并在收到回执后核对订阅。演示切换需等前一个运行窗口退出，两种模式都使用端口 18080。

```powershell
pwsh -NoProfile -File tools/build-spring-sandbox.ps1
.venv/Scripts/python.exe -I -B -X utf8 tools/verify-spring-workbench-deploy.py
```

[Java 实现](apps/supportflow/spring-sandbox) · [Java Compose](deploy/supportflow/compose.spring-java.yaml) · [回执对账](apps/supportflow/jobs/grant_reconciliation.py)

真实 DeepSeek 与 Java 联调演示中，客服完成一次已见诊断、打开知识引用，再发起一次明确授予。独立 SQL 核验了批准后增加 10 席、有效期 3 天、订阅版本由 7 变为 8，并且只产生一份回执。脚本模型下的故障回归另做了三轮提交后丢响应实验。[演示与回归批次](docs/verification.md)

## 版本与验证范围

本仓库提供对应开发提交 `8cb9a34d` 的运行快照，含运行源码、依赖锁、演示夹具、前端与 Java 测试。该开发版本的 B14869 批次通过了 1,752 项 Python 离线测试。完整 Python 测试集与原始运行档案保存在开发工作区。[版本与验收记录](docs/verification.md)

原版 v9 在冻结提交 `d4e82ee` 上登记了 534 个正式单元，404 项通过、102 项失败、28 项执行错误。后续修复复验单独登记，正式分母和原始结果均保留。[逐项正式结果](docs/verification/v9-final.json)
