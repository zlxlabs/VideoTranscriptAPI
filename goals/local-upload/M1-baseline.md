---
lane: local-upload
id: M1
slug: baseline
status: 实现中
owner: delegate implementer dlg-20261008-094534-0fd910
order: 1
priority: 高
depends_on: []
merged_pr: null
---

# 里程碑进度：local-upload/M1：上传身份、分享资格与清理基线

- **预期产出**：cache.db 持有独立上传身份/期限/撤销记录；旧 NOT NULL `view_token` 不再承载公开上传 token；实际 Resolver 与 cache/task/audit/temp 维护消费者按资格保护。
- **当前范围**：A 卡 guard-only 基线。不包含 HTTP 接收、真实 ASR worker、网页、生产额度和恢复证明；未合并、未部署、未启用。
- **关键决策**：用现有 `CacheManager` 做 store；`view_token=''` 只作旧列占位，公开 `upload_` token 只从新表解析；`VTA_UPLOADS_ENABLED` 缺省/非 true 即关闭读取，不影响清理保护。
- **推进前必须拿到的证据**：
  - [x] 卡树 `uv sync --frozen` 后，窄测连续五次：`uv run --frozen pytest -q tests/unit/test_local_upload_policy.py tests/unit/test_view_token_resolver.py tests/cache/test_task_status_cleanup.py`，五轮 exit=0；环境：本地隔离 worktree 与临时 SQLite，日志 `/tmp/vta-upload-A-narrow-final-{1..5}.log`。
  - [x] `make test` 完整 gate exit=0；URL Resolver、API、history、cache/task cleanup 与媒体锁竞态整文件回归共 196 tests，exit=0；环境：当前 card worktree。全量日志 `/tmp/vta-upload-A-make-test-final.log`。
  - [ ] Resolver 开关测试同时经过当前进程和真实子进程，使用同一份临时 SQLite 验证变量缺省与显式 true；CI 和裸 shell 需运行。当前仓无 upload systemd consumer；不得把本地结果声称为 systemd/生产验证。
  - [ ] 独立审查与 PR run/job 实际 conclusion；draft 的 SKIPPED 不算 primary 通过。
- **完成条件**：上述证据全部来自真实 Resolver 与维护入口，代码位置/测试锁定成对；PR merge 之前由 lead 验收。生产启用仍依赖 M2–M6 的部署、容量及恢复门。
