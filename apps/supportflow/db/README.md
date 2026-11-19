# SupportFlow 数据库迁移与本地 seed

从项目根运行入口。解释器需安装 `uv.lock` 中的依赖，并能通过内部网络连接本项目的 PostgreSQL。共享主机的安装、构建和数据库实测遵循项目运行窗口约定。

| 参数 | 含义 |
| --- | --- |
| `--target app` / `--target sandbox` | 分别选择应用库、沙箱库自己的 Alembic 历史及迁移角色 |
| `PGHOST` / `PGPORT` / `PGDATABASE` | 默认 `postgres` / `5432` / `sf_app` 或 `sf_sandbox`。隔离实测可指定 `sf_eval_<标识>_app` 或 `sf_eval_<标识>_sandbox` |
| `PGPASSFILE` 或 `PGPASSWORD` | libpq 所用迁移角色凭据。用户名固定为 `sf_app_migrator` 或 `sf_sandbox_migrator` |
| `SF_SEED_PASSWORDS_FILE` | 首次应用库 seed 所需的私有 UTF-8 JSON 文件，键为每个本地账号的 username，口令至少 16 个字符且至多 1024 个 UTF-8 字节。单独生成，保存在 Git 忽略目录，并限制主机权限 |

在各目标对应的连接环境中运行下面的命令。第二行的输入可以使用本仓库 `evals/fixtures/supportflow/fx_base/seed.json` 中的合成数据。

```text
python -B -m apps.supportflow.db.manage migrate --target app
python -B -m apps.supportflow.db.manage seed --target app --fixture <seed.json>
python -B -m apps.supportflow.db.manage migrate --target sandbox
python -B -m apps.supportflow.db.manage seed --target sandbox --fixture <seed.json>
```

应用库保存租户、账号、成员角色、客户和显式客户授权。相同客户编码可出现在不同租户中。角色和客户分别用包含租户的外键绑定到授权。沙箱库单独保存订阅。导入前校验两侧的租户／客户引用，再在所选库的一次事务中写入全部数据及输入文件 SHA-256。

迁移和 seed 在同一数据库使用相同的事务级 advisory lock。相同 fixture 身份、版本和字节的重复 seed 直接读取 `seed_history` 返回；已有业务状态、已撤回的客户授权和已轮换的密码均按当前记录保留。同身份同版本但不同摘要会被拒绝；与现有行冲突的首次导入也回滚。两个库独立提交。若第二库中断，修复后可用原输入重跑，第一库会读回已提交记录。
