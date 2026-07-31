# 藏知备份与恢复

知识导出和系统备份解决不同问题：

- “设置 → 数据与备份”的 ZIP 导出用于阅读、迁移和长期留存知识内容；
- 系统备份用于完整恢复藏知，必须同时包含 PostgreSQL 数据库与 `storage/`。

`storage/` 中既有原文件，也有加密 AI Key 所需的 `.secret_key`。只备份数据库而
遗漏该目录，会导致原文件和已保存的模型密钥无法恢复。

## 创建并校验备份

```bash
scripts/backup.sh
scripts/verify-backup.sh backups/cangzhi-YYYYMMDD-HHMMSS
```

也可以把备份写到挂载的外部磁盘：

```bash
scripts/backup.sh /mnt/backup/cangzhi
```

每个备份目录包含：

- `database.dump`：PostgreSQL custom-format dump；
- `storage.tar.gz`：原文件、网页快照和加密主密钥；
- `metadata.json`：代码版本、数据库迁移版本和资料数量；
- `SHA256SUMS`：完整性校验。

## 隔离恢复演练

```bash
scripts/rehearse-restore.sh backups/cangzhi-YYYYMMDD-HHMMSS
```

演练只会使用带当前进程编号的临时数据库 `cangzhi_restore_check_<PID>`，不会停止
服务，也不会修改正式数据库。脚本恢复完成后会读取迁移版本和资料数量，随后自动
删除临时数据库。

## 正式恢复原则

正式恢复属于破坏性运维，当前不提供“一键覆盖”按钮。执行时应：

1. 停止 Web、API 和 Worker，保留 PostgreSQL；
2. 再为当前环境做一份紧急备份；
3. 在明确的空数据库中恢复 `database.dump`；
4. 将 `storage.tar.gz` 解压到项目目录，确认包含 `storage/.secret_key`；
5. 运行 Alembic 升级到当前代码要求的版本；
6. 启动服务，检查资料数量、随机下载原文，并验证模型配置可解密；
7. 检查活动向量索引覆盖率；必要时从设置页重建索引。

正式覆盖恢复应由管理员在服务器终端完成，避免网页误操作清空当前数据。
