# SDK 传输替换审查判决

failure-visibility: clean

审查范围：`ed0ffccfc008091cb874de5473fedf1e931f9697..4afea9781066aec876b3fc1c15c24611de313686`
风险等级：personal
判决：未发现违反任务卡不变式的问题；P1：无。

## 来源（补录，2026-10-02）

- 本判决由 review 卡在分支 `card/vta-asr-sdk-review` 上产出（commit `925b7490`），该分支从未合并，因此判决在主干查不到；按「审过了必须能在 git 里查到」补录，**上方判决正文逐字未改**。
- 审查区间 `ed0fccc..4afea97` 对应已合并的 PR #96「用官方识别客户端替换手写传输」（`card/vta-asr-sdk`，2026-09-30 合并），区间内 3 个实质提交为 `eb27154a` 钉死官方 SDK 依赖、`d017c0d8` 改用官方 SDK 传输、`77c93e6a` 收紧 SDK 重试契约。
