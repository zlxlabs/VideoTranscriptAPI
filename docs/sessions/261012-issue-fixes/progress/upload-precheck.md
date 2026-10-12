# 上传最终提交既有合成主干预检记录（verifying）

- 日期：2026-10-12。对象：父上传卡最终 H0；仅运行既有 accept_precheck 工具，未评审/修改生产代码。
- 范围：`c7dc7fcbb1727057ecf2c4d8c3f38e34d3136f72..db60c385410030fc2e34def510c3c8ab1e0e5117`（6 提交，覆盖原 B 全部提交）。

## 命令（通用路径）

```text
python3 ~/.local/lib/agent-config-runtime/current/scripts/delegate/accept_precheck.py \
  --dispatch-id dlg-20261012-135924-bf492d \
  --repo-path <upload-release worktree> \
  --commit-range c7dc7fcbb1727057ecf2c4d8c3f38e34d3136f72..db60c385410030fc2e34def510c3c8ab1e0e5117
```

## 结果（accept_precheck.json schema v2，checked_at 2026-10-12T15:06:39+00:00）

- 顶层 status：green，退出码 0。head_sha=`db60c385410030fc2e34def510c3c8ab1e0e5117`；main_sha_at_check=`c7dc7fcbb1727057ecf2c4d8c3f38e34d3136f72`；identity_source=commit-range（6 提交）；content_fingerprint=`8404c33d0010600f382f53a15610e9eeb884cf89`。
- scope：green —— 5 个变更文件均匹配 Scope-Follow 声明（tests/README.md）。
- verify_on_merged_main：green，退出码 0；`TMPDIR=<短私有tmp> make test`，进度点号至 100%，无失败与信号行。
- protected_paths：green —— 无保护路径或软链/子模块模式命中。
- ci（只作线索）：green，各检查项 skipped；post_merge=not_merged。
- advisory：red_on_base=unknown（be1fb1ca 反向应用失败，判据不可用）；report_claims=red（父报告缺「闸与绕过/与卡面的偏差」标题）；accept_check=green（2/2 通过）。
- auto_lane：tests-v1，eligible=false（5 项判据未满足），仅记录。

## 结论

- 阻断性检查（scope / verify_on_merged_main / protected_paths）全绿：既有合成主干预检通过，不阻断。
- advisory 中 red/unknown 不阻断，如实留档，是否接受交主脑裁量；未改脚本、未调参数、未签豁免。
