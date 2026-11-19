# 本地知识切片

这些 31 份 Markdown 是手工编写的合成业务资料，不代表生产知识库。`corpus.json` 有32个租户条目，分别登记两个租户、普通／私有资料、未激活旧说明、干扰项和注入文本；完整临时席位policy被两个租户分别登记。只有 `reviewed_sections.json` 的完整 section 正文 hash 允许提供对应支持；topic不授予审批或业务写权限。新提案图另核代码固定policy常量、当前scope和实际业务观察。产品模块不导入 `evals/`。

管理员可在 `/knowledge` 上传 MD／TXT 并发布选定版本。上传先生成 staged 版本。发布接口 `POST /api/v1/documents/{id}/versions/{version}/activate` 接收 `expected_generation`，首次构建填 `null`。接口用现有本地模型和租户锁构建两路索引，发布事务复核当前权限和索引代。版本列表显示正在构建、发布完成、失败和构建中断，失败时继续读取先前有效版本。正在构建的同版本请求可返回 202，过时索引代返回 409。此入口在 API 的有界线程中构建，独立导入 worker 和正式质量／容量评测仍待后续阶段。seed 中的旧说明是 staged 示例，历史版本由实际发布记录产生。

以下命令在已登记运行窗口的本项目内部网络执行。迁移和身份 seed 先完成；CLI 使用 `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`（默认 `sf_app_runtime`）、`PGPASSWORD_FILE`。不要把凭据值写入命令。共享目录由 `SF_KB_UPLOAD_DIR`／`SF_KB_SNAPSHOT_DIR` 指定，模型目录使用 `SF_EMBEDDING_MODEL_DIR`。

```text
python -B -m apps.supportflow.knowledge.cli seed --tenant tenant_a --actor user_a_admin --corpus-tenant primary --output /evidence/seed-a.json
python -B -m apps.supportflow.knowledge.cli build --tenant tenant_a --actor user_a_admin --overrides-file /evidence/seed-a.json
python -B -m apps.supportflow.knowledge.cli seed --tenant tenant_b --actor user_b_admin --corpus-tenant secondary --output /evidence/seed-b.json
python -B -m apps.supportflow.knowledge.cli build --tenant tenant_b --actor user_b_admin --overrides-file /evidence/seed-b.json
```

首次发布省略 `--expected-generation`；后续必须填当前代，例如 `--expected-generation 1`。`--overrides-file` 也接受裸 `{document_id: version_id}` JSON，只替换明确指定的文档，其他有效文档继续纳入同代。部分构建失败后使用相同输入及 `--resume-generation` 复用已持久化的向量，不能把不完整代发布。改变模型／tokenizer／chunker 时创建新代。

映射值 `null` 明确停用该文档：新代不包含它，在发布事务中清除其 active version；原版本、旧代向量与 ACL 保留，因此仍可按当前权限读取历史引用。不通过删除向量伪造停用。不能发布完全为空的索引。

普通部署可调用 `ensure --tenant tenant_a --actor user_a_admin --corpus-tenant primary`（另一次为 secondary）。它只为产品 catalog 指定的文档做幂等 seed；当前版本、已固定模型／分词版本、双 ready、manifest 和快照都一致时读回现有代，不重新 embedding。否则以读到的当前代为 expected 进行一次 CAS 构建。显式选择这个 bootstrap 会恢复 catalog 指定的产品示例版本；自建文档继续保留，私有 ACL 撤销不会因重复 seed 被重新授予。

构建连接必须 autocommit，以保证每个短事务真正释放；tenant advisory lock 跨各短事务持有。原始上传使用生成路径；向量精确搜索，无 ANN；BM25 使用已持久化词频并在当前授权候选中计算统计；两路各 top20 经 RRF(60) 取最多 5 条。查询发生代／ACL变化最多补查一次，消耗同一 run 的工具与时间预算。检索模型投影最多 1500 个锁定 tokenizer 的 tokens，超限明确失败而不截断证据。

模型是固定 revision 的本地 CPU ONNX；`retrieval/asset_pins.json` 是代码侧公共 asset 校验表，下载 manifest 不能自行改 pin。默认生产入口无假向量开关；测试注入假向量必须同时位于 eval profile 和隔离应用库。真实向量也不自动获得正式质量实验资格，bundle 的 `quality_eligible` 当前保持 false。
