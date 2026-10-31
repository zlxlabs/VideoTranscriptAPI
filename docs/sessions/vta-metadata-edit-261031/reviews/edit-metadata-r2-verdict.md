# edit_metadata 增量与全量二审：H1

<!-- delegate-outcome: succeeded -->

failure-visibility: p2-only

结论：F1 已闭合，未发现满足 personal 两问的 P1；本审不阻断交付。保留 F2/F3 两条 P2，以及 F4/F5/F6 三条 P2 候选，不宣称十条不变式在所有交错下成立。未改实现，未标 ready 或合并 PR。

## 固定范围与独立性

- 全量：`8d4d7372101b58781a8b637b3a40d2a23742a8fb..3903215c9e659f7c48374f9619ebc5d0aa74e4f5`。
- 增量：`6a3ad1d5bd76b6b71fd20ee0d172cc438975579e..3903215c9e659f7c48374f9619ebc5d0aa74e4f5`。
- 风险档 personal：P1 仅数据丢失、静默出错、崩溃；第一问须有真实使用触发测量。下列受控交错/故障注入证明机制，不冒充自然发生统计。
- 初始 HEAD 为上述 H1，工作树干净；执行器 codex / gpt-6.1-sol / high，Dispatch-Id `dlg-20261031-061245-f63361`，Task-Id `VideoTranscriptAPI-20261031-04`。
- 全量核对所有 11 个功能相关生产/配置/指南/测试文件，逐字节对照 H1，得到 `H1_source_match=11/11`。允许读取 r1 verdict，但本轮每条独立重判。实现与修复报告、会话、stdout 均未读取；两个旧 progress 文件属于隔离的自述，未读正文。
- 批量只读调研预算 12 轮，入口为固定 SHA 的 git diff/show、rg、定向源码读取；不查后续 HEAD、不检索会话、不构建图、不派子代理。额外范围闸排查只读检查器源码，不进入被审实现范围。
- 原始缓存 E=`/tmp/vta-edit-metadata-r2-evidence/`；`h1-reviewed.diff` SHA256 为 `e23484a04574b0974b589a7f48d4397acfb9c64d609928d8b216df11aa2ab73e`。探针、JSONL 与 pytest 日志均在 E，不入库。
- OCR 状态按卡面为 `reviewed_fallback`：主腿 leg_timeout，备腿成功；未重新调用 OCR，不把 verifier confirmed 当独立证据，也不写成 skipped 或 clean。

## 独立验证命令与摘要

运行 cwd 为本审 worktree 根，Python 3.11.15，生产源码严格为 H1。原命令退出 0：

```bash
uv run pytest -q tests/unit/test_edit_metadata.py tests/unit/test_user_manager_permissions.py --tb=short
```

原命令因仓库重复 quiet 不打印计数，整行摘要原文：

```text
...........................................                              [100%]
```

仅覆盖 addopts 补取计数，以及验证全量 diff 的错误码消费者：

```text
uv run pytest -q tests/unit/test_edit_metadata.py tests/unit/test_user_manager_permissions.py --tb=short -o addopts=''
43 passed, 143 warnings in 1.11s
uv run pytest -q tests/unit/i18n_api/test_error_codes.py --tb=short -o addopts=''
203 passed, 408 warnings in 0.63s
```

探针统一命令：`uv run python /tmp/vta-edit-metadata-r2-evidence/probe.py <case>`。真实 FastAPI 路由、真实 UserManager 权限判断、真实 CacheManager 和 AuditLogger 临时 SQLite；只替换认证依赖的身份，故障/交错个案另明确注入。非空 task_audit_snapshots 与身份列在普通案例前后逐值比较。无生产库、外部网络或真实凭据操作。

## 增量专项：F1 是否闭合与新回归

- 生产增量仅 `_clean_optional` 从空白返回 None 改为抛 400 `metadata_invalid`，并补充说明；测试新增两个组合输入用例。另有 r1 审查文档纳入历史，不是新运行时路径。
- `blank_title`：`title=" \t "`、合法 author；实得 HTTP 400 `metadata_invalid`，两表为 Old Title/Old Author，公开页同值。
- `blank_author`：合法 title、`author=" \n "`；实得同样 400，两表与公开页均不变。对应 H1 `TestInvalidBody.test_blank_title_with_valid_author_400` 和 `test_valid_title_with_blank_author_400` 独立通过。
- F1 闭合：两个清理调用均在 resolver/任何元数据写入前；第二个字段失败时第一个字段也尚未写入。无 200 部分写入。
- 省略字段由 `TestPartialEdit` 覆盖；`null_omission` 实测 title=null、合法 author 为 200，title 保留旧值。无把 null 当空白的回归。
- 四问：仅修登记 F1；无新抽象；无新增状态/事实源/fallback；未留下新旧双路径。全量两组 43 测试通过，未发现增量新回归。

## Findings：本轮独立重判

### F2 / P2：PNG 枚举后消失，把已提交更新报成失败

- 位置：`src/video_transcript_api/api/services/metadata_editor.py:137`、`:158`、`:159`；违反不变式 9。
- 触发：`probe.py png_disappears`，临时 PNG 在原 unlink 前被删除，再调用原 unlink；输出 HTTP 500 / Internal Server Error，两表及公开页均为 New Title/New Author。普通 `normal_no_png` 为 200。
- 后果：一次 HTTP 失败与已提交数据库不一致。
- 代价：若后续决定处理，文件不存在的幂等删除约 1 行，0 新抽象；不吞其他 I/O 错误。
- P1 两问：机制注入已复现；自然删除竞争的发生频率未实测。数据库值正确、无已证数据丢失或持续崩溃；维持 P2，不阻断，不因首审或工具标签升 P1。

### F3 / P2：ownership 捕获 SQLite 错误并转换为业务错误

- 位置：`src/video_transcript_api/api/routes/metadata.py:146`、`:148`；违反不变式 10 的 SQLite 失败上抛与禁止新增防御式捕获要求。
- 触发：`probe.py ownership_sqlite`，在调用真实 ownership 前删除临时 audit 表；原 SQL 失败，HTTP 503 `ownership_check_unavailable`，两表仍为旧值。该个案之后 resolver 因同一缺表返回 None，不作为写入证据。
- 后果：原 SQLite 异常被 except Exception 转换；拒绝写入且错误对用户可见。
- 代价：可删捕获块，0 新抽象/分支；本卡不修。
- P1 两问：故障注入能触发，未测得真实库损坏；失败可见且无越权写入，不命中 personal 红线，维持 P2、不阻断。

### F4 / P2 候选：检查与更新间可新增在途任务，随后旧元数据覆盖编辑

- 位置：`src/video_transcript_api/api/routes/metadata.py:158`、`:168`；`metadata_editor.py:52`、`:105`；违反不变式 4，影响 5/8 的成功结果。
- 触发：`probe.py new_inflight`，原约束检查结束后通过真实 create_task 插入同媒体 queued 任务，再返回原路由；输出 HTTP 200，`statuses_at_success=["success","queued"]`。随后真实 save_cache 写入流水线旧标题/作者，公开页变回 Old Title/Old Author。这是受控调度，未执行下载/转录/LLM，也不是生产事故统计。
- 后果：检查快照不能保证写入时仍无在途任务；后续缓存生产者可覆盖元数据。
- 代价：尚不请求新增锁；如处理需同时分析 create_task 准入与更新的边界，预估 20–60 行调整及交错验证。仅在路由持 media_lock 不能约束不持该锁的 create_task，不把工具的“加锁”直接当解法。
- P1 两问：机制实测成立，正常 API 两请求自然重叠/真实流水线覆盖未实测；后果若自然发生属于静默覆盖，但第一问证据不够，按卡面仅 P2 候选、不阻断。

### F5 / P2 候选：清理删掉缓存行后仍返回 200

- 位置：`src/video_transcript_api/api/services/metadata_editor.py:112`、`:126`、`:131`；违反不变式 5/8。
- 触发：`probe.py cleanup_between_check_write`，原检查后把临时缓存年龄设为过期并调用真实 cleanup_old_cache，断言删除 1 行；输出 HTTP 200，`cache=[]`，task_status 已更新，页面 `status=file_cleaned,title=New Title,author=null`。没有用假 UPDATE 返回值代替真实 SQLite。
- 后果：video_cache UPDATE 命中零行仍报成功，公开页缺失成功体中的 author。
- 代价：如处理，旧行存在性/实际更新数的失败检查预估 4–8 行，无新身份或 fallback；是否与清理共用既有锁须另验证，不能凭此添加机制。
- P1 两问：边界注入成立，真实编辑撞定时清理的频率未实测；无新增删除动作（删除来自既有正常清理），错误成功存在但自然触发证据不足，P2 候选、不阻断。

### F6 / P2 候选：expired 快照让在途任务绕过第 4 条

- 位置：`src/video_transcript_api/api/services/metadata_editor.py:63`、`:66`、`:69`；违反不变式 4 的字面范围。
- 触发：`probe.py expired_inflight`，建立同媒体 processing 兄弟任务并通过真实 audit API 标为 expired；输出 HTTP 200，`statuses_at_success=["success","processing"]`，两表已改。最终证据为 `probe-expired-final.jsonl`。
- 后果：第 3 条的“未过期”限定被同时应用到第 4 条；卡面第 4 条没有这项豁免。本审显式提出该矛盾，不默认为规格允许。
- 代价：若坚持字面契约，可调整在途判断与 expired 分支顺序，预估 2–5 行；无新抽象，不在本卡实现。
- P1 两问：临时库构造可复现；真实系统是否能持久出现 expired+processing 未实测，不能据此升 P1。P2 候选、不阻断，需主脑明确规格/实际状态约束。

## 不变式逐条对账（所有行号属于 H1）

| # | 代码与锁定证据 | 本轮结论/限制 |
|---|---|---|
| 1 | user_manager.py:17、metadata.py:103；TestPermissionDenied、TestPermissionCheck、真实权限探针 permission_recalibrate/admin/delete | 各旧权限单独授予均 403 metadata_permission_denied，库/页不变；不复用权限，通过。 |
| 2 | metadata.py:53、:63、:107；TestInvalidBody/TestUrlForbidden、blank_title/blank_author/null_omission/max_length/extra | F1 闭合；JSON null 保留旧项；201 字与额外 download_url 均 422。长度 200 由 Field 上限约束；无单独边界 200 测试，不声称完整边界覆盖。 |
| 3 | metadata.py:103、:139、:158；metadata_editor.py:62；TestOwnershipDenied/TestSharedMediaDenied、null_sibling、legacy_null_sibling | 非 legacy NULL 兄弟行 403 shared，库不变；正确 legacy 身份加 NULL 兄弟为 200；首版 legacy 用 user_a 数据导致 403，已纠正夹具独立重测，不当缺陷。检查后的新归属变化未测试保证。 |
| 4 | metadata_editor.py:27、:69；TestInFlightRejected 参数三状态 | 普通在途均 409 且两表不变；并发窗口见 F4，expired 例外见 F6，不能写全面通过。 |
| 5 | metadata_editor.py:105–135；CacheManager._get_cursor:239；TestSpeakerVariantCoverage、rollback | 两表 UPDATE 同事务；第二表 trigger ABORT 后 500 且两表均旧值；speaker=0 页实际读 speaker=1 且编辑后新值，已有测试锁定。缓存行消失例外见 F5。 |
| 6 | metadata_editor.py:118–135；普通探针比较非空 snapshots 全行和两表 URL/身份列 | title/author 外不改；非空审计快照保持原值；路由仅记录 API 调用，不写 task snapshots。调用链没有下载/转录/LLM；无独立计数断言，按代码核对说明限制。 |
| 7 | metadata.py:113；TestUploadTokenRejected | upload_ 请求 404 transcript_not_found，不能进入更新，通过。 |
| 8 | metadata.py:191–198；ViewTokenResolver.get_view_data_by_token:326；TestSuccessfulEdit/TestPartialEdit | 普通成功 200，响应 data 三字段与公开页新值一致；并发/清理限制见 F4/F5。未要求 message。 |
| 9 | metadata_editor.py:137、:141；TestOgPngInvalidation、normal_no_png | zh-CN/en PNG 被删；起初无 PNG 仍 200；枚举后消失见 F2。 |
| 10 | 两个新模块；rollback、ownership_sqlite | 无 overlay、新身份系统、重试；写事务 SQLite 错误上抛并回滚。ownership 新 catch 违反上抛要求，见 F3；不为此要求新增异常信封。 |

## OCR 标注与 personal 两问对照

| 工具输入 | 独立证据与本仓判定 | 两问/交付 |
|---|---|---|
| 检查/写入分事务，未持媒体锁 | F4 受控 queued 插入及真实 save_cache 覆盖成立；不把持锁形态当缺陷证据 | 自然重叠未测；潜在静默覆盖不可接受，但第一问未满足，P2 候选，不阻断。 |
| UPDATE 不查 rowcount | F5 真实 cleanup 删除 1 行后 200、cache 空 | 自然撞清理未测；成功结果不完整，P2 候选，不阻断。 |
| 成功体缺 message/response_model | 实测 normal_no_png 为 200，code/data 含 view_token/title/author，公开页相符 | 第 8 条不要求这两项，无法关联不变式；仅接口风格候选，不认定运行时 finding，不阻断。 |
| 首审 F2/F3 | 本轮重新做 png_disappears/ownership_sqlite，未沿用原定级作证 | 都仅注入触发、自然发生未测，失败可见或库值正确；P2，不阻断。 |

## 熵增、测试边界与交付约束

- 两个新增模块 metadata.py/metadata_editor.py 在卡面允许范围；前者被 app 注册，后者被路由调用。单消费者服务的必要理由是集中两表事务与提交后文件副作用，不再建议新包装层。
- `_clean_optional` 有 title/author 两个调用点；请求模型由 FastAPI 消费；`_invalidate_og_pngs` 单消费者，理由为分离事务后的文件删除，不为它再提取抽象。在途常量与存量同值，没有实测语义漂移，至多熵增候选。
- 新增测试穿过真实 HTTP 与 SQLite；权限响应与认证身份用替身，故真实 UserManager 权限由本轮探针另验。组合回归读两表和真实公开页 resolver，能观察 F1 的结果；本审不做生产代码变异，不宣称完成 TDD 红验。
- expired 探针首版错误地把造数时的 expire 当成接口改审计，AssertionError 原始日志保留在 probe-extra.stderr；纠正比较范围后重跑，不拿这次断言失败冒充生产缺陷或红验。
- 主干基线不可用，继承红未能判定；上述本地验证无新红。P2 不阻断是风险分诊，不能解释成无规格差异或所有并发均安全。
- 当前审查只交付指定 verdict/progress；提交及推送、工作树和范围闸的实际回执写到本派发 report.md，避免文档自引用提交 SHA。
