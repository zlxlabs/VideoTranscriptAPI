# edit_metadata 独立首审：H0

<!-- delegate-outcome: succeeded -->

failure-visibility: p1-found

结论：发现 1 条 P1（F1，静默接受不合法的组合输入），阻断交付；F2/F3 为 P2，不单独阻断。执行器已完成审查，未修实现。

## 范围与取证

- 固定范围：`8d4d7372101b58781a8b637b3a40d2a23742a8fb..6a3ad1d5bd76b6b71fd20ee0d172cc438975579e`。本文所有生产/测试行号均属于 H0，不追后续提交。
- risk-tier: personal；P1 仅数据丢失、静默出错、崩溃。规格只使用任务卡十条不变式。
- 初始 `git status --short --untracked-files=all` 输出为空；`git rev-parse HEAD` 为 `6a3ad1d5bd76b6b71fd20ee0d172cc438975579e`。
- 当前 worktree 独立运行 Python 3.11.15；逐字节比对 H0 的 11 个生产/配置/指南/测试变更文件，输出 `H0_source_match=11/11`。
- 完整审阅上述功能 diff，消费路径仅用于解释 diff；未读实现方报告、stdout、会话或修改动机。唯一排除的 diff 文件是实现方 `progress/edit-metadata-progress.md`（68 行会话自述），遵守输入隔离。
- 批量只读调研预算：首轮 6 次批量调用，随后追加 4 次定向读取预算；入口为固定 SHA 的 `git diff/show`、`rg`、定向源码读取，无远程调研、无知识图构建、无子代理。
- 原始证据缓存：`$DELEGATE_REPORT_PATH` 所在目录下的 `reviewer-evidence/`（下称 E）。`h0-reviewed.diff` 的 SHA256 为 `8957fa09bfbfda4c485f51168e4166c835607b2e8b7b1dc4e7af0de982f20ffb`。
- OCR 输入状态为 `reviewed_fallback`（任务卡提供），不是 skipped；未自行重跑，也未把 verifier confirmed 当作本审结论。

## 独立测试与复现入口

在本文记录的 worktree 根目录执行卡面原命令：

```bash
uv run pytest -q tests/unit/test_edit_metadata.py tests/unit/test_user_manager_permissions.py --tb=short
```

实际退出码 0；原始整行进度摘要如下（仓库 addopts 含第二个 `-q`，因此原命令不打印计数尾行）：

```text
.........................................                                [100%]
```

仅覆盖 addopts 以取回计数摘要，再执行同一组用例：

```bash
uv run pytest -q tests/unit/test_edit_metadata.py tests/unit/test_user_manager_permissions.py --tb=short -o addopts=''
41 passed, 139 warnings in 1.12s
```

补验本 diff 改动的错误码消费测试：

```bash
uv run pytest -q tests/unit/i18n_api/test_error_codes.py --tb=short -o addopts=''
203 passed, 408 warnings in 0.73s
```

探针命令（`E` 展开为上述绝对路径；原始输出见 `probe-v2.log`）：

```bash
uv run python "$E/probe.py"
# 单独重现：可在末尾指定下面的 case 名，如 blank_title_valid_author
```

探针加载本树既有测试的配置预热和造数入口，经真实 FastAPI 路由、真实 UserManager 权限判断、真实 CacheManager 和 AuditLogger 临时 SQLite；只替换认证依赖的用户身份，不替换权限/归属判断。每例先建立非空审计快照，核对实际 HTTP 响应、两表内容及公开页 resolver 输出。无真实凭据、外部网络、生产数据库或服务操作。

## Findings

### F1 — P1：空白字段与有效字段同传被静默接受

- 位置：`src/video_transcript_api/api/routes/metadata.py:63`、`:100`；违反不变式 **2**。
- 触发：上述探针 `blank_title_valid_author`，实际请求字段 `title=" \t "`、`author="New Author"`。真实输出摘录：`"http": 200`；响应 `title="Old Title", author="New Author"`；`cache=[["Old Title","New Author"]]`、`tasks=[["Old Title","New Author"]]`；公开页同值。
- 对称触发：`valid_title_blank_author`，`title="New Title"`、`author="\n "`，实际也是 200，作者保留 Old Author、标题已更新。
- 后果：应返回 400 `metadata_invalid` 的请求进入成功写路径，已提供的空白字段被当成缺省忽略，另一字段写入并向调用方报成功。
- P1 两问：①真实使用方式能触发吗？能，直接对本树 HTTP 入口发送正常 JSON 组合输入已实测，无故障注入。②后果可接受吗？否，输入校验静默失效，成功响应掩盖了请求中字段未被接受，命中 personal 的静默出错红线。
- 最小处置建议：先分别检查每个已提供字符串的 strip 结果，再处理缺省；代价约 2 个条件、4–8 行，无新抽象。既有 `TestInvalidBody` 只测单独空白字段，漏掉“一空一有效”组合。
- 阻断交付：是。

### F2 — P2：枚举后 PNG 消失导致已提交的写入被报失败

- 位置：`src/video_transcript_api/api/services/metadata_editor.py:137`、`:159`；违反不变式 **9**。
- 触发：探针 `png_disappears_after_glob`。在真实临时 PNG 被枚举后、unlink 前注入一次文件消失，再执行原始 unlink；实际输出 `"http": 500, "response": "Internal Server Error"`，而 `cache=[["New Title","Old Author"]]`、`tasks=[["New Title","Old Author"]]`，公开页已显示 New Title。
- 后果：数据库已提交，PNG 恰好不存在却让 HTTP 调用报失败。普通“起初无 PNG”对照 `trim_and_no_png` 为 200，不能用它否认删除窗口问题。
- P1 两问：①本树注入式复现成立；真实使用中该删除竞争的发生频率未实测。②后果是单次错误响应与提交结果不一致，可通过读取公开页确认，未证实持续崩溃/丢数据。因此不升级 P1。
- 最小处置建议：仅让 unlink 的文件缺失成为幂等操作，例如 `missing_ok=True`；代价 1 行、0 新抽象，不吞其他 I/O/SQLite 错误。
- 阻断交付：否；作为 P2 记录，不单独派修复卡。

### F3 — P2：新路由捕获 SQLite 异常并替换成业务错误

- 位置：`src/video_transcript_api/api/routes/metadata.py:130`、`:139`；违反不变式 **10**（SQLite 失败上抛、禁止新增防御式捕获）。
- 触发：探针 `ownership_sqlite_failure`，在进入真实 ownership 函数前删除临时 audit.db 的 `task_audit_snapshots` 表，原函数实际执行 SQL；输出 `"http": 503`、`"error_code": "ownership_check_unavailable"`，两表仍为 Old Title/Old Author。
- 后果：原 SQLite 失败被新引入的 `except Exception` 转成 ApiError；没有越权写入，失败向用户可见。
- P1 两问：①故障注入后确能走该捕获；未证实实际运行库存在这类损坏。②失败可见且未写入，不命中数据丢失/静默出错/崩溃红线，故为 P2。
- 最小处置建议：删除新增的异常转换，让底层失败上抛；代价为删减捕获块、0 新分支/抽象。
- 限定：初版过早删除表只走到既有 resolver 的吞错路径并返回 404，不能作为新捕获块的证据；已将注入点移到 ownership 调用处并复测，以上结论仅据第二版。
- 阻断交付：否；作为 P2 记录。

## 不变式逐条对账

| # | H0 代码位置 | 测试/探针与结论 |
|---|---|---|
| 1 | `user_manager.py:17`、`metadata.py:96` | `TestPermissionDenied`、权限测试通过；探针分别只授 recalibrate/admin/delete，均 403 `metadata_permission_denied`，两表不变。通过。 |
| 2 | `metadata.py:53–68,100–103` | `TestInvalidBody`/`TestUrlForbidden` 通过；额外 platform 和 201 字标题探针均 422、两表不变；混合空白输入失败，见 F1。 |
| 3 | `metadata.py:96,131,151`；`metadata_editor.py:53–88` | `TestOwnershipDenied`/`TestSharedMediaDenied` 通过；NULL 归属单任务 403 ownership，已归属任务加 NULL 兄弟行 403 shared、未写；legacy 单 token 对照 200。通过；OCR 的 NULL 指控与卡面相反。 |
| 4 | `metadata_editor.py:27,69,81` | `TestInFlightRejected` 逐测 queued/processing/calibrating，均 409 `metadata_in_flight` 且两表不变。通过既有用例；检查到写入之间的并发插入没有被测试锁定，不声明并发原子准入。 |
| 5 | `metadata_editor.py:103–135`；`cache_manager.py:239` | `TestSpeakerVariantCoverage` 锁定 speaker=0 页实际读取 speaker=1 的前置条件及更新后页面；回滚探针在第二表 UPDATE 触发 SQLite ABORT，HTTP 500，两表均保留旧值。通过。 |
| 6 | `metadata_editor.py:120–135`；`metadata.py:86` | SQL 只 SET title/author；探针核对 task_status 的 url/download_url/platform/media_id 不变、非空 task_audit_snapshots 全列不变。video_cache 身份列未作独立快照断言，代码中无其写入；无下载/转录/LLM 调用。通过代码核对，说明测试限度。 |
| 7 | `metadata.py:106` | `TestUploadTokenRejected` 通过，upload_ 请求 404、未进入写入。通过。 |
| 8 | `metadata.py:175–191`；`view_token_resolver.py:326` | `TestSuccessfulEdit`、`TestPartialEdit` 及 trim 探针为 200，响应三字段和公开页数据吻合。合法成功路径通过；不覆盖 F1 的错误成功。 |
| 9 | `metadata_editor.py:137–160` | `TestOgPngInvalidation` 删除 zh-CN/en PNG；无 PNG 对照 200；文件消失窗口失败，见 F2。 |
| 10 | 新增路由/服务全部 | 无 overlay、新身份系统、重试；写事务 SQLite 失败回滚并上抛。ownership 新增 catch 不符合，见 F3；既有 resolver 吞错仅作依赖限制记录，不扩大本轮修复范围。 |

## OCR 输入与本仓 P1 两问重判

以下是本审独立核验后的分类，不将 OCR 的 verifier 结果视为证据：

| 工具候选 | 本仓判定 | 两问答案与依据 |
|---|---|---|
| 权限配置与指南清单不一致 | 文档候选 C1，不阻断 | `multi_user_setup.md:49` 确只列三种旧权限；可造成配置困惑，但未见数据/崩溃后果。卡面没有指南同步不变式，不能冒充 #1 的权限运行时缺陷。 |
| NULL submitted_by 被拒绝 | 驳回 | NULL 兄弟行探针实得 403 shared，库不变；这恰好落实 #3 的归属不明按拒绝。改为放行反而违反卡面。 |
| unlink 文件消失窗口 | F2 / P2 | 注入复现真实 unlink 的 500；未实测自然发生频率，不能因工具标注升 P1。 |
| service 两字段均 None 拼非法 SQL | 驳回 HTTP finding | `TestInvalidBody.test_both_fields_missing_400` 已实跑通过；路由先返回 400，此输入到不了唯一生产调用点。直接调用 service 未实测，无需新增内部防御校验。 |
| 空白 title 配有效 author | F1 / P1 | 实际 HTTP 200 且两表部分写入；正常输入可触发、静默接受违反 #2，不可接受。 |
| 在途常量重复 | 熵增候选 C2，不阻断 | 与既有 `NON_TERMINAL_STATUSES` 当前同值，无运行时失效证据；卡面无共用常量要求，不为此新增抽象。 |
| SQLite 未映射 ApiError JSON | 驳回 | 第二表 UPDATE 注入实得 500 且完整回滚，这是 #10 要求的失败上抛；不应要求新 catch。另见 F3，现有 ownership 捕获恰好违背上抛要求。 |

## 熵增与规格边界

- 允许的两个新模块：`metadata.py` 被 `app.py` 注册并由路由测试消费；`metadata_editor.py` 被路由消费并由这些 HTTP/SQLite 用例验证。没有 overlay、依赖或配置层。
- 请求模型有 FastAPI 消费者；`_clean_optional` 被 title 与 author 两处调用，但语义错误已归入 F1，不建议再包一层。
- `_invalidate_og_pngs` 只有 `apply_metadata_update` 一个生产消费者；无第二消费者，记熵增候选 C3。它承担事务提交后的文件副作用，目前不要求为 P2 调整结构或新增包装层。
- 规格显式疑点：#10 要求 SQLite 失败上抛，与 OCR“必须映射 ApiError”冲突；按卡面拒绝该工具建议。#3 明确 NULL 非 legacy 拒绝，与 OCR 相反，已做双向归属对照。#2 未明确 JSON null 是否等于省略，本审未自行扩张为 finding。
- 并发准入窗口仅作未锁定的不变式候选 C4：三次检查与写入不是一个事务；未实测并发交错，不判 P1、不提出锁或重试机制。
- 主干基线未提供，继承红未能判定；本地两组指定测试及错误码补验无红。审出 P1 不等于执行器任务失败。
