# card-vta-261015-speaker-evidence 进度存档

## 段1：现场核查与设计定稿

- **当前阶段**：implementing（现场核查完成，开始落码）
- **本段结论**：base 95d21bb5 上建分支 card/vta-261015-speaker-evidence；真实 fixture 已入库 `tests/fixtures/twitter_space_1wxWjlzQVwDJQ/transcript_funasr.json`（sha256 cf6346ae4046e75037e36aee6b2cfaae0c33db6d3785bb7e1db0ab768e0986fc，201407 字节，383 段、11 说话人，与卡面附注一致）。裁决设计定稿：evidence_type 域值 {dialog_address, title_credit, description_roster, author_host}；quote 核对对象为最终组装的 user_prompt 全文（`quote in prompt`，不做任何归一化）；裁决顺序 dialog_address+title_credit 复合 > 仅 dialog_address > 其余一律不 applied；description_roster 不独立 applied（程序无法机械验证「与对话内容对应」，保守处理为仅记录佐证，理由见 report）。
- **关键决策与已否决方案**：缓存命中重放按 meta 形状分叉——v2 条目（含 candidates 键）直接采用缓存 mapping/applied（证据无法离线重核，输入文本不在缓存里）；v1 形状保持旧 confidence-gate 重判（= 完成条件④「走旧降级语义」选项）。这使 test_speaker_inferencer.py 既有缓存命中测试（v1 形状 mock）零改动全绿。已否决：v1 产物强制迁移（会破坏既有缓存重判测试，且无行为收益）。
- **下一步唯一动作**：落 schema v2 与 prompt 分区，跑 Narrow-Verify 相关用例。

## 段2：核心实现全部落地，全量测试除继承红外全绿

- **当前阶段**：implementing → 收尾（验证完成，待写报告）
- **本段结论**：六个 commit 完成 schema v2 + prompt 分区（a5a3b48c）、证据裁决/指纹/top2 加额（a3dacad5）、真实 fixture 验收测试 16 条（54f49a25）、消费点同步 + envelope v2（0feb51c0）、集成 seed 同源修复（058b5478）。红验两项均转红后还原：①引用核对判据去掉 quote 成员检查 → test_forged_quote_rejected_to_placeholder 红（AssertionError: '魂蓝（对话称:某大V）' != '说话人2'）；②指纹 canonical 的 description 字段置空 → test_metadata_change_changes_fingerprint 红（AssertionError: 两个 sha256 相等）。make test 终态：3778 passed / 1 failed（test_asr_monitor_alert_delivery.py::TestNetworkGuard::test_unix_domain_socket_is_not_blocked，AF_UNIX path too long，已在 base 95d21bb5 复现同为红，继承环境红非本卡责任）。
- **关键决策与已否决方案**：卡面 Accept-Check 第 2 条原命令 `uv run python -c "from video_transcript_api..."` 在 base 上同样 ModuleNotFoundError（包不经 pip 安装，tests/conftest.py 注入 sys.path）——等价形式 `PYTHONPATH=src uv run python -c ...` exit 0 通过，记入报告。description_roster 的「quote 与对话内容直接对应」无法机械验证，落地为永不独立 applied（仅佐证记录），report 说明。
- **下一步唯一动作**：写 report.md 并 push 分支。
