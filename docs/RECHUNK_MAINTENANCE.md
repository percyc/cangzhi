# 显式切片重建维护

适用阶段：未正式大规模使用，维护者接受旧引用 ID 变化与短暂向量空窗。
这不是新产物原子激活功能；正式上线后应优先完成 CZ-N02。

## 操作顺序

1. `make backup`，再 `make verify-backup BACKUP=backups/cangzhi-...`。
2. 由维护者更新 Worker 到包含 `chunking:assisted-v1` 的代码，不需要数据库迁移。
3. 只读预览首批：

   ```bash
   docker compose exec -T worker python -m apps.worker.rechunk --ids 664 671 389 7 8
   ```

4. 确认备份校验成功、维护窗口和目标后增加 `--apply --backup-verified`。
   命令仅排队；Worker 执行切片后继续排队向量，不表示命令退出时向量已完成。
5. 查看收件箱与数据库任务状态，确认首批切片及当前向量覆盖完成，再验证真实问题。
   本机既有样本可用 `python3 scripts/check-rechunk.py` 比较检索；
   `python3 scripts/check-fragment-context.py --deployed` 验证一个实际快速回答，
   加 `--deep` 验证深度回答。固定 ID 脚本只适用于本机，其他实例应替换自己的测试集。
6. 验证不退步后使用 `--all --limit 10` 预览下一批，再加 apply 参数提交。
   每批最多 20 份，不自动无限循环。先观察覆盖和失败，再扩批。

### 旧队列暂停期间

维护者要求停止旧流程时，先让常驻 Worker 收到 TERM 并正常退出；不使用 KILL，
不把残留 processing 批量重置。失败和未解析资料不自动重新提交。
只启动一次性 Worker 命令处理选中的新策略任务：

```bash
docker compose run --rm --no-deps worker python -m apps.worker.rechunk \
  --ids 664 671 389 7 8 --run --max-jobs 100 --backup-verified
```

它只处理选中文档的维护切片任务及其新策略向量，旧 parsing/stored 和旧向量任务
不会被领取；单次最多 1000 个任务，遇失败停止本批。再次执行会继续未完成部分。
常驻 Worker 停止期间新上传仍可保存，但后台处理暂不推进，不把停机描述为永久取消
旧任务。恢复常驻 Worker 前必须明确处理旧队列，否则它仍会继续消费旧任务。

## 处理和风险边界

- 只选择活动空间、未回收、当前 ready 的正文文档；非当前版本不处理。
- 正在执行的任务和等待中的非向量阶段会阻止该文档入选；不重置 processing 任务。
- XLS/XLSX/数据库表保持数据集目录和精确执行，不因为本次维护生成逐行片段。
- 普通 Word/Markdown/网页/随手记保留类型 Profile；PDF 候选最多两次模型调用，
  其余窗口使用规则，不称为全文 AI 分析。诊断记录在版本的 `assisted_chunking` 中。
- 同版本同维护策略仅提交一次，失败由既有任务重试流程处理，不重复制造任务。
- 当前流程先事务替换切片，再排队向量；旧 chunk ID 及相应向量会被删除。
  原文件、raw_content、structured_content、分类和标签不被此命令修改。
- 回滚本次数据变更需要维护者按备份恢复流程处理；只回滚镜像不能还原旧 chunk ID。
- 命令不输出文档正文、密钥或凭证。`--backup-verified` 是操作者确认，不能替代备份校验。
