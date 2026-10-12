<!-- delegate-outcome: succeeded -->
# CI238 ready 基础设施取证（dlg-20261012-173038-f3ea33，纠正轮）

只读诊断，未改 src/tests/.github/配置。执行器：pi executor。

## 现场
- head `d52bd95043e3151be00be6beb452b68287c38873`（branch card/vta-261012-deploy-signal-test）；job 内 checkout 的 `c34df6fa` 是 refs/pull/238/merge 合并提交，非产品内容差异。
- run38278729107（gate）：quality 114910492183 / primary 114910607981 / gate 114921032798 success。

## 失败结论（job 级，非 run 级）
1. **Browser chromium job114910489777（run38278728173）**：run 级结论 completed/**failure**
   （head d52bd950，updated_at 2026-10-12T16:47:26Z；主脑实时 GET 权威记录。本执行器前轮一次
   API 响应曾记录 success，与权威记录矛盾，按权威修正，不据此推断聚合/覆盖语义）。
   job 级失败：setup-uv@v8.1.0 抓
   `https://raw.githubusercontent.com/astral-sh/versions/main/v1/uv.ndjson` 后 `fetch failed`
   （3.1s；uv 未 pin 版本 → fallback latest 才需该 manifest）；底层原因未知（日志无细节）。
   同 head draft run38278531015 success 仅旁证该步骤可通过，非环境等价/因果完全确定。
2. **ledger job114921803941（run38278729107）**：本体 `Upload v2 review effectiveness ledger` 已成功，
   仅最后一步 `Upload v2 review effectiveness ledger latest pointer` 拒：
   `Silo put failed: S3Error: IncompleteBody`——只证服务端称体长不足，底层根因未知
   （传输短读/超时/中断与客户端实现均未排除）。事后 net-diag 全绿只证明探针时点可达，
   不能据此断言非网络/凭据或确定客户端 bug。与 Task09/11/13 收取判定层无关，
   主脑远端 GET 的 accepted 结论不受影响。
3. **OCR job114910711117（run38278729107，advisory）**：`SOURCE-CLIENT-FAILED: exit=4
   GIT-SOURCE-PREPARE-V1 SOURCE-PREPARE-FAILED`（VideoTranscriptAPI@c34df6fa，source=null，3.3s）；
   同 job `zlxlabs/gate` service checkout reason=ok → 服务可达，仅本仓源准备失败。
   无 structured finding，不推 clean。

## 分类
- 继承红 vs 新红：主干基线不可用，三处均「未能判定」；main 近 6 run 仅 gate-disposition（success @10-08），无同构对照。
- 责任出处（跨仓，只记不 AC）：git-source-prepare exit=4 语义与 silo_store latest-pointer put 均在 zlxlabs/gate 注入源，本仓无源码。

## 下一步（不在本卡执行）
- browser 红：fetch failed 底层未知，候选方向 pin uv 版本或查 raw.githubusercontent.com 可达性。
- ledger 红：zlxlabs/gate 侧核 latest-pointer 上传 IncompleteBody 的底层根因（客户端与服务端两侧均未排除）。

完整证据：三份失败 job 全量日志已留存于 delegate 现场私有目录（不在公共仓）。本取证完成不代表 CI 通过。
