# CI238 ready 基础设施取证（dlg-20261012-171710-7ec63f）

只读诊断，未改 src/tests/.github/配置。执行器：pi executor。

## 现场
- head `d52bd95043e3151be00be6beb452b68287c38873`（branch card/vta-261012-deploy-signal-test）；job 内 checkout 的 `c34df6fa` 是 refs/pull/238/merge 合并提交，非产品内容差异。
- run38278729107（gate）：quality 114910492183 / primary 114910607981 / gate 114921032798 success。

## 失败结论（job 级，非 run 级）
1. **Browser chromium job114910489777（run38278728173）**：setup-uv@v8.1.0 在
   `Fetching manifest data from https://raw.githubusercontent.com/astral-sh/versions/main/v1/uv.ndjson`
   后 `##[error]fetch failed`（3.1s 快速失败）。uv 未 pin 版本 → fallback latest 才需要该 manifest。
   同 head draft run38278531015 同 job success → 间歇性网络，非确定性 head 问题。
   注意：该 run run 级 conclusion=`success`，run 绿不聚合 chromium job 红——判 CI 只看 job 级。
2. **ledger job114921803941（run38278729107）**：本体 `Upload v2 review effectiveness ledger` 已成功，
   仅最后一步 `Upload v2 review effectiveness ledger latest pointer` 拒：
   `Silo put failed: S3Error: IncompleteBody`（Content-Length 与实际字节不符）。
   同 job net-diag 全绿（silo_http=http_200）→ 非网络/凭据；与 Task09/11/13 收取判定层无关，
   主脑远端 GET 的 accepted 结论不受影响。
3. **OCR job114910711117（run38278729107，advisory）**：`SOURCE-CLIENT-FAILED: exit=4
   GIT-SOURCE-PREPARE-V1 SOURCE-PREPARE-FAILED`（VideoTranscriptAPI@c34df6fa，source=null，3.3s）；
   同 job `zlxlabs/gate` service checkout reason=ok → 服务可达，仅本仓源准备失败。
   无 structured finding，不推 clean。

## 分类
- 继承红 vs 新红：主干基线不可用，三处均「未能判定」；main 近 6 run 仅 gate-disposition（success @10-08），无同构对照。
- 责任出处（跨仓，只记不 AC）：git-source-prepare exit=4 语义与 silo_store latest-pointer put 均在 zlxlabs/gate 注入源，本仓无源码。

## 下一步（不在本卡执行）
- browser 红：候选 pin uv 版本或查 raw.githubusercontent.com 在自建 runner 的间歇可达性。
- ledger 红：zlxlabs/gate 侧核 silo_store latest-pointer 上传的 Content-Length 计算。

完整证据：三份失败 job 全量日志已留存于 delegate 现场私有目录（不在公共仓）。本取证完成不代表 CI 通过。
