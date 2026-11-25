# SupportFlow

「同事收到了邀请，为什么还是加不进来？」

我做了一个模拟 SaaS 客服工作台来排查这类工单。客服能查邀请有没有过期、邮箱域名是否允许、客户还剩几个席位。诊断由 LangGraph 编排，回答里引用了政策，点开就能看到当时用的是哪一版、依据在哪几行。

**整个工作台只有一个业务写动作，授予临时席位。** 客服在工单里写清要给哪个客户加多少席、用几天，另一位主管用自己的账号审批。即使已经批了，执行前也要再查权限和订阅版本。比如等审批的这段时间里客户已经扩过容，旧申请就停在这里。

## 席位加了，响应却丢了，怎么办？

我在 Spring Boot 联调里专门测了这个时刻。Java 刚在同一个 PostgreSQL 事务里加完席位、写好回执，测试代理就截断 HTTP 响应。Python 没收到结果，先把这次执行标成待核对。worker 拿原来的 `action_id` 查回执，读到后再核对订阅。第二轮连查两次都是 `404`，第三次才拿到回执。这期间没有再发加席请求。[Python 对账逻辑](apps/supportflow/jobs/grant_reconciliation.py) · [Java 写入事务](apps/supportflow/spring-sandbox/src/main/java/io/supportflow/sandbox/GrantStore.java)

这三轮故障注入固定了模型回复，每轮换一个动作。**每轮 1 次写请求、1 份持久回执、0 次重复授予。** 我还对 Java 接口同时发过 8 个相同动作、相同参数的请求，得到 1 个 `201`、7 个 `200`。八个响应指向同一份回执，数据库里的订阅版本只从 7 变到 8。[Java 接口与工作台回归记录](docs/verification.md)

## 跑起来

- Windows x64、PowerShell、Git，以及能运行 pip 的 Python。
- Node 24.14.0、npm 11.9.0。启动 Docker Desktop，使用 Linux 容器。
- 首次安装要联网下载依赖和嵌入模型。从仓库根目录执行下面三行。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/setup-env.ps1 -Stage Install
New-Item -ItemType Directory -Force .local | Out-Null
.venv/Scripts/python.exe -I -B tools/prepare_embedding.py --output .local/embedding-setup.json
```

要跑完整工单，把自己的 DeepSeek API 密钥单行保存在本机 `deploy/supportflow/.secrets/deepseek_api_key`。启动脚本会生成演示账号密码。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/serve-workspace.ps1 -Minutes 15 -WithWorker -WithKnowledge -WithProposal -WithApproval -WithExecution -WithDemoData
```

用两个独立浏览器会话打开工作台。分别登录客服 `a_support` 和主管 `a_supervisor`，密码在本机 `deploy/supportflow/.secrets/seed-accounts.json`。只想看登录和工单界面，可以把启动参数缩成 `-Minutes 5`，无需模型密钥。到时服务自动停止，数据卷保留。[打开本机工作台](http://127.0.0.1:18080/tickets)

**可以直接试这张工单。** 以客服身份为 C-001 建工单，问「member@example.com 加不进来，帮我查一下邀请、域名和席位」。点开一条引用看原文，再为这位客户明确申请 10 个临时席位、有效期 3 天。若当前订阅符合政策，切到主管会话审批，再回客服页看回执。想看断线续传，可以在运行期间让浏览器短暂离线，再恢复连接。[提案规则](apps/supportflow/domain/grant_policy.py) · [浏览器如何接着读事件](apps/supportflow/web/product/event_client.ts)

<details>
<summary>切到 Spring Boot 业务服务，或单独跑前端测试</summary>

默认写入 Python 沙箱。切换 Java 时，等上一轮服务退出，在完整启动命令末尾加 `-WithJavaSandbox`。两种模式都用 18080 端口。Java 有独立的 PostgreSQL 业务卷。要单独检查 Java 构建和部署配置，准备能编译 Java 21 的 JDK（联调使用 JDK 25），执行下面两行。

```powershell
pwsh -NoProfile -File tools/build-spring-sandbox.ps1
.venv/Scripts/python.exe -I -B -X utf8 tools/verify-spring-workbench-deploy.py
```

前端可以单独构建并运行测试。

```powershell
npm.cmd --prefix apps/supportflow/web ci --no-fund --no-audit
npm.cmd --prefix apps/supportflow/web run build:product
$ui = @(Get-ChildItem tests/supportflow -Filter 'ui_*.test.mjs' | Sort-Object Name | ForEach-Object FullName)
node --test @ui
```

</details>

## 踩过的坑

- 第一次采集 Java 联调结果，容器连 `apps.supportflow.tools` 都导入不了。漏挂的是 Python 工具目录，采集器还没走到业务断言。
- 补好挂载，又碰到一句 `Cannot open a client instance more than once.`。采集代码对同一个 HTTP 客户端打开了两次，修的是客户端的生命周期。
- 另一次，浏览器已经拿到了诊断引用，下一步提交加席申请却超时了。后来让浏览器先在同一张工单上重读服务端的完成状态，再发申请。新一轮才走通从审批到回执的全程。[联调验收记录](docs/verification.md)

## 检索找到一条依据，就够回答吗？

19 道固定查询上，纯向量检索有 15 道能在前五条里命中至少一条依据，加上 BM25 做混合检索后是 18 道。但要把一道题需要的依据**全部找齐**，两种策略分别只有 7 道和 6 道。所以我保留了两个指标。两次越权读取引用的检查也都被拒绝。[逐题检索结果](docs/verification/sf-ri.md#检索)

工作台的「评测」页还能看到原版 Python 沙箱的 13 个写入场景，每个场景查了三轮数据库。39 轮里重复变更为 0，有 3 轮是提交后按预期取消。[可靠性记录](docs/verification/sf-ri.md#可靠性) · [打开本机评测页](http://127.0.0.1:18080/evaluation)

原版 v9 最终入账 534 个单元，404 个通过、102 个失败、28 个执行错误。目前尚无独立盲测准确率。[冻结结果及修后复验](docs/verification/v9-final.json)
