# vta-261031-edit-metadata 进度存档

## 2026-10-31 段1：现场核查与机制摸底

- **当前阶段**：implementing（TDD 红-1）
- **本段结论**：现场干净（分支 card/vta-261031-edit-metadata，基线 8d4d7372 与卡面一致）。
  机制摸底完成：公开页 title/author 走 `ViewTokenResolver._assemble_content_view` 读
  `video_cache`（`ORDER BY use_speaker_recognition DESC`），OG PNG 新鲜度 = png mtime >=
  llm_share_card.json mtime，删 png 即强制重绘。发现轴表疑点：OG headline 来自
  llm_share_card.json（LLM 产物），DB 只贡献 author——按卡面锁定决策照做，疑点写进报告。
- **关键决策与已否决方案**：实现顺序定为 权限白名单红测 → 路由红测 → 实现 → 全量绿；
  `metadata_shared_denied` 优先于 `metadata_in_flight`（按卡面锁定决策文序）；
  old→new 走 logger.info 结构化一行，不污染 log_api_call 的结构化列。
- **下一步唯一动作**：提交 tests/unit/test_edit_metadata.py 红测（路由模块不存在，14 用例 ImportError 红），然后实现。

## 2026-10-31 段2：主测红

- **当前阶段**：implementing（TDD 红-2 已提交）
- **本段结论**：主测 tests/unit/test_edit_metadata.py 落盘，覆盖卡面全部 11 个场景
  （含 speaker=0/1 变体覆盖与 OG PNG 失效）。红-1 实测：`UserConfigError: user entry
  1.permissions contains an invalid permission`；红-2 实测：`ImportError: cannot import
  name 'metadata' from ...routes`。两个红均为预期类型。
- **关键决策与已否决方案**：测试只 mock verify_token Depends 与 UserManager 的
  check_permission，ownership/约束/写入全部走真实 CacheManager + 真实 AuditLogger
  （卡面端到端要求）；error_code 断言依赖 install_api_error_handlers 注册。
- **下一步唯一动作**：实现转绿（已提交），跑 make test 全量 + 红验 + scope check，然后 push。

## 2026-10-31 段3：实现绿

- **当前阶段**：implementing（Narrow-Verify 绿）
- **本段结论**：`uv run pytest -q tests/unit/test_edit_metadata.py
  tests/unit/test_user_manager_permissions.py` => `41 passed, 1.63s`。实现五件套：
  ALLOWED_PERMISSIONS 加 edit_metadata；metadata_editor.py（约束检查 + 单事务双 UPDATE
  + OG PNG 清理）；routes/metadata.py（四道闸 + upload_ 拒绝 + url 422）；app.py 注册；
  i18n 五个错误码中英双份 + users.example.json 示例。
- **关键决策与已否决方案**：editor 直接抛 ApiError（照 services/transcription.py 惯例，
  否决了 services→routes 反向 import 的初稿）；ownership 异常复用 503
  ownership_check_unavailable 现有错误码；测试 seed 补 task_status 终态写回
  （create_task 的 INSERT 本身不写 title/author，DB 未改断言才覆盖两表）。
- **下一步唯一动作**：make test 全量回归。

## 2026-10-31 段4：全量绿、守卫级联、红验

- **当前阶段**：verifying（全量绿 + 红验完成）
- **本段结论**：make test exit=0、[100%] 无 F/E；初跑红在两个既有 i18n 守卫（错误码
  参考文档 stale + 手写目录缺新码），走 delegate scope-request seq=1 批准后登记
  `test_error_codes.py` 5 行并重新生成 `docs/guides/api/error-codes.md`，守卫绿。
  红验两枚均有效：① video_cache UPDATE 加 `AND use_speaker_recognition = 0` →
  TestSpeakerVariantCoverage 红（'Speaker1 Title' != 'Both'，AssertionError）；② 权限闸
  `if False and ...` → TestPermissionDenied 红（assert 200 == 403）。均 cp 备份/单点还原，
  还原后 41 passed。
- **关键决策与已否决方案**：红验用最小注入（只改判据行），不用全局替换；make test 在
  重定向下不输出 pytest 摘要行（-q 叠加本仓插件行为的既有现象），全绿判据取 exit=0 +
  [100%] 全点。
- **下一步唯一动作**：push 分支，写 report.md。

## 2026-10-31 段5：交付文档（部署顺序）

- **当前阶段**：done
- **本段结论**：上线顺序（同 routes/metadata.py docstring）：
  1. 先部署本卡代码（新路由 + ALLOWED_PERMISSIONS 白名单）；
  2. 再往生产 `config/users.json` 给目标 key 的 permissions 加 `edit_metadata`（未跟踪
     运行时文件，不在本卡范围）。
  回滚顺序反向：先把 `edit_metadata` 从 users.json 删掉，再回滚代码——
  `_validate_users` 对未知权限名抛 UserConfigError 拒绝整份配置，新配置 + 旧代码会
  打挂全部用户鉴权。
- **关键决策与已否决方案**：无。
- **下一步唯一动作**：push。
