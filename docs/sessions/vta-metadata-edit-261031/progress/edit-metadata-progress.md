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
- **下一步唯一动作**：提交 tests/unit/test_user_manager_permissions.py 的红测（edit_metadata 白名单）。
