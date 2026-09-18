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

## 新上传优先（维护调度）

维护时不能直接恢复普通 Worker 来处理新文件，否则旧队列也会被恢复。专用调度器
以显式 UTC 时间点区分新版本，旧资料只有明确列出的维护 ID 可以进入后台队列。
判断依据是文档版本创建时间，不是任务创建时间，避免旧资料刚创建的重试任务插队。

前台新资料与后台维护按 4:1 的任务机会分配；一侧没有到期任务时让另一侧使用空位。
新资料内部继续按阶段轮转，避免不断上传时只解析、不切片，或大量向量挤占新解析。
这是任务间的优先权，不会强行中断正在运行的 OCR/模型请求，也不保证固定等待秒数。
新 PDF 的切片任务采用受限辅助策略；普通正文和数据集保持各自处理逻辑。
旧失败任务、不在授权列表中的旧资料、非当前版本、回收和归档空间均不领取。

代码交付后，由维护者构建包含新入口的 Worker 镜像，在普通 Worker 停止、旧的一次性维护执行器退出后启动。
不要直接执行 `make upgrade` 恢复普通 Worker，否则旧队列也会继续运行。
以下命令不会自动更新镜像，启动前必须确认镜像中包含本次代码：

```bash
docker compose run -d --no-deps --name cangzhi-priority-worker worker \
  python -m apps.worker.priority_maintenance \
  --new-since 2026-09-18T08:51:50.105133+00:00 --history-ids 7 8 389 664 671
```

时间和 ID 是本机维护清单，其他实例应使用自己的界限；不填时间时必须拒绝启动。
本入口只调度已经存在的任务，不自动发起全库重建，不修改公共上传/MCP 契约。

当前交付状态（2026-09-18）：用户明确授权后，`27688c3` 已合并推送 main，维护优先
入口已启动，镜像 `cangzhi-worker:priority-release-20260918`（`fa53c6cc240e`）。
隔离测试 850 项、发布镜像专项 40 项通过；真实新资料 1013 的 5 个任务已全部完成。
首批 5 份资料的 732 条向量已完成并通过内容哈希核对；
五问检索抽查仍为 4 问完整覆盖，法律 Word 的既有召回缺口尚未解决。
本次没有排队下一批历史资料，优先保证新上传处理，再逐批观察历史重建。

本机容器 `cangzhi-priority-worker` 已设置 `unless-stopped`，截止时间保持上述值，
历史名单只包含首批 5 份。不启动普通 `cangzhi-worker`。查看状态可用
`docker inspect --format '{{.State.Status}}' cangzhi-priority-worker`；健康检查仅验证数据库，
还应查看收件箱任务推进。停止维护入口使用 `docker stop --time 600 cangzhi-priority-worker`，
当前任务很长时应等其结束后再停止，不能以强制终止作为正常取消。
回退时先停止维护入口，保留旧队列暂停；旧镜像标签为
`cangzhi-worker:rollback-before-priority-20260918`，不包含优先入口，不能直接用其恢复普通
Worker。新上传处理暂停不影响原文件保存。部署前联合备份
`backups/cangzhi-20260918-100748` 已校验；镜像回退不等于切片数据恢复。

前台只按明确截止时间判断；首次启用应使用暂停时间，重启时保持同一时间，避免漏掉
暂停期间已上传的资料。不传 `--history-ids` 即仅处理新资料；该参数是文档 ID，
不是任务 ID。两个维护优先实例通过 PostgreSQL 会话锁互斥，但不能阻止普通 Worker
同时运行。TERM 会在当前任务结束后退出，强制终止仍可能留下 processing，不能自动
批量重置。超过辅助策略块数上限的 PDF 回退类型规则并记录诊断，不丢弃原文。

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
