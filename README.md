# SupportFlow｜SaaS 工单受控写操作 Agent

SupportFlow 是面向模拟双租户 SaaS 的客服工作台。客服从工单查邀请、订阅与服务事实，查看带版本和行号的知识引用。临时席位授予需主管批准具体提案；执行前重新核对权限和业务版本，提交后按回执对账。

Python · FastAPI · LangGraph · PostgreSQL / pgvector · Celery / Redis · React / TypeScript · Spring Boot（可选业务服务）

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

## Spring Boot 业务服务

在完整演示命令末尾加 `-WithJavaSandbox`，并准备支持 Java 21 字节码构建的 JDK（本次使用 JDK 25）。脚本用锁定的 Maven 构建 Java 模块。Java 使用独立 PostgreSQL 业务卷；Python 沿固定内部地址读取事实、批准后派发，并在收到回执后核对订阅。演示切换需等前一个运行窗口退出，两种模式都使用端口 18080。

```powershell
pwsh -NoProfile -File tools/build-spring-sandbox.ps1
.venv/Scripts/python.exe -I -B -X utf8 tools/verify-spring-workbench-deploy.py
```

[Java 实现](apps/supportflow/spring-sandbox) · [Java Compose](deploy/supportflow/compose.spring-java.yaml) · [回执对账](apps/supportflow/jobs/grant_reconciliation.py)

本快照对应开发仓提交 `5d55ea35c873bf99d34211024cc20e8a052347f8`，含应用、依赖锁、启动夹具和可独立运行的 Java 与前端测试。[验收范围、版本与证据批次](docs/verification.md)
