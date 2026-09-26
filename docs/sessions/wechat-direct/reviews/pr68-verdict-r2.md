# PR #68 独立审查 verdict（R2：对抗性运行时实测）

- **审查对象**：`git diff 4e0a5ab0e8841b07577a2d9e984a078f18a6f68c..b2d67c3`（H0 冻结；之后的新提交不属于本轮）
- **H0 SHA**：`b2d67c3d39a7788a0245d6523ff3271d95b4439a`
- **风险等级**：internal（P1 红线：数据丢失、静默出错、崩溃、越权访问、损坏他人数据）
- **真实使用方式**：单机部署，resolver 为自有服务，视频号 URL 来自用户提交。本轮在审查 worktree 所在机器上实测：`169.254.169.254` 的 AWS IMDS v1 / v2 token / GCP metadata 三个 URL 均 `curl -m 2` 连接超时（http=000），本机无云元数据服务。
- **Verdict 文件**：`docs/sessions/wechat-direct/reviews/pr68-verdict-r2.md`（本文件）
- **审查人**：Grok（独立 review 卡 dlg-20260902-125054-3011bf，全新会话，未见实现方报告）
- **与 R1 的关系**：R1 是静态 diff + 单测 + OCR，verdict **pass / 无 P1**，P2-1 为「CDN GET 跟随 30x、redirect 目标未过 `validate_url_safe`」。本轮**不重复读 diff**，只用真实本地 HTTP stub 驱动 `MediaResolverDownloader.download_file` 的真实 `requests` 路径，确认或推翻该判定，并覆盖其余不变式。

## 本轮新证据是什么

1. 在 `b2d67c3` 上起真实 `ThreadingHTTPServer`（`127.0.0.1` 高端口），同时扮演 resolver（`GET /api/stream/wechat_channels/{sph}/direct`）与 CDN（Range / 302 / TCP 掐断）。**未使用 `unittest.mock`**。
2. 仅把 `VTAPI_CONFIG` 里的 `media_resolver.base_url` 指向 stub、`api_key` 设为测试钥；CDN 要走真实 `validate_url_safe`，故把 `127.0.0.1` 写入 `security.download_url_allowlist`（生产已有的白名单机制，不是 mock）。`169.254.169.254` **不在**白名单。
3. 用真实 `MediaResolverDownloader().download_file(stream_url, filename)` 跑 6 个对抗场景；stub 侧记录每个请求的 path / headers / 状态；loguru sink 捕获真实日志。
4. 跨文档：`docs/guides/media_resolver.md`（本 diff）对照上游 `~/projects/work/MediaResolverAPI/README.md` 的 `/direct` 与「客户端拼接协议」节。
5. 本轮未重跑 OCR：同一份 H0 diff 已在 R1 `ocr-review` 得到 `status=reviewed`；对同一 diff 再扫不算新证据。

Harness 与原始结果在 `/tmp/pr68-r2-run/`（不入库）：`results.json`、`scenario1_fixed.json`、`routing.json`。

## 不变式速览（本轮实测）

| # | 不变式 | 本轮结论 | 关键实测 |
|---|--------|----------|----------|
| 1 | token 保密 | ✅ INFO/异常干净；⚠️ DEBUG 存量泄露 | 场景 6 |
| 2 | 密钥隔离 | ✅ | 场景 5：resolver 必带 `X-API-Key`，CDN 从不带 |
| 3 | 完整性 | ✅ | 场景 2：掐断后续传，落盘字节 == 期望全文 |
| 4 | 有界失败 | ✅ | 场景 2/3/4：失败抛错并销毁临时文件，不截断凑数 |
| 5 | 路由隔离 | ✅ | 匹配 stream path 才进新路径；`/direct` 后缀与错误 netloc 不进 |
| 6 | SSRF | ⚠️ 初始 URL 有效 / 302 目标无效 | 场景 3 拦截元数据直链；场景 1/1b **跟随** 302 且不对 Location 再校验。确认 R1 P2-1，不升级 P1 |

---

## 场景 1：CDN 响应 302 到另一个 stub 路径

**注入是否生效**

- stub `/direct` 返回 `cdn_url=http://127.0.0.1:<port>/cdn/token-R2CDNTOKENAAAA1111-redir.mp4`（含时效 token 字面量）。
- stub 对该 path 回 **302** `Location: http://127.0.0.1:<port>/cdn/redirect-target`。
- 第一次跑时 harness 误把 `/cdn/redirect-target` 也匹配成 302（路径含子串 `redir`），出现 30 次自环后 `TooManyRedirects` → `CDN connect failed`。这只能证明「跟随了」，不能证明「跟随后会不会把目标正文写进文件」。
- 修正 sink 后复测（`/tmp/pr68-r2-run/scenario1_fixed.json`）：stub 记录

  | path | status | 次数 |
  |------|--------|------|
  | `.../SPHREDIR01/direct` | 200 | 1 |
  | `/cdn/token-R2CDNTOKENAAAA1111-redir.mp4` | 302 | 1 |
  | `/cdn/redirect-target` | 206 / 32768 bytes | 1 |

  第二次 CDN GET 的 `Range: bytes=20-` 被转发到 redirect-target。注入生效，路径完整走到跟随后的 206。

**观察到的行为**

- `download_file` **跟随** 302（`requests.get` 默认 `allow_redirects=True`，代码未关）。
- 跟随后的 206 正文被写入临时文件；下载成功返回路径；落盘 32788 字节且 `equals_full=true`。
- 全程 **1 次** `/direct`（没有因为 302 本身去换链——跟随成功后视作一次合法 206）。
- redirect-target **没有**再过 `validate_url_safe`（日志里只有对原始 token URL 的 `URL safety check passed`）。

**判定（不变式 6）**

确认 R1 P2-1，**不推翻**。行为是「跟随并采用目标正文」，不是「拒绝 30x」。详见 Findings P2-1。

---

## 场景 1b（加强）：CDN 302 到 `http://169.254.169.254/latest/meta-data/`

**注入是否生效**

- stub 对 `/cdn/token-...-redir-meta.mp4` 回 302，`Location` 为元数据 URL。
- stub 记录该 302 发出 2 次（首次 + 一次换链重试）。CDN 侧没有出现对 stub 其它 path 的 206。

**观察到的行为**

- `elapsed_s = 20.0365`（CDN connect timeout 为 `(10, 60)` 的 10 秒连接超时 × 2 次零进展）。
- 日志：`wechat direct CDN connect failed` → refresh `/direct` → 再次 GET 原 token URL → 再 302 → 再 10s 超时 → `DownloadFailedError: wechat direct no progress ... offset=20`。
- 对比场景 3（直链就是 169.254.169.254）仅 **0.0039s** 即 SSRF 拒绝：证明 1b **确实对 Location 发了 TCP 连接**，而不是在校验层拦住。
- 最终无落盘文件（`file: null`），临时文件被丢弃。

**判定**

运行时确认：对已通过 SSRF 的 `cdn_url`，其 302 Location 可以是链路本地元数据地址，代码会尝试连接。本机 IMDS 不可达，后果是失败而非读到元数据。不升级 P1，理由见 Findings。

---

## 场景 2：CDN 流在 50% 处 TCP 掐断，换链后续传

**注入是否生效**

- 第 1 次 `/direct` 下发 `token-R2CDNTOKENAAAA1111`。
- stub 对该 URL 回 206，`Content-Range: bytes 20-32787/32788`，`Content-Length` 广告 32768（剩余全文），但只写出 **16384** 字节后 `shutdown(SHUT_RDWR)` + `close()`。
- stub `sent_status`：`abort_after=16384, advertised=32768`。
- 第 2 次 `/direct` 下发 `token-R2CDNTOKENBBBB2222`；对该 URL 完整 206。

**观察到的行为**

- 日志：`CDN stream interrupted sph_code=SPHABORT50 offset=20 gained=16384`。
- 换链：`refresh CDN link ... offset=16404 refreshes=1`（20+16384=16404）。
- 第二次 CDN 请求头 `Range: bytes=16404-`（stub 实测）。
- `download complete ... bytes=32788`；落盘文件 `equals_full=true`，`head_ok=true`。
- `/direct` 调用 2 次，CDN 两个 token 各 1 次。无截断凑数。

**判定（不变式 3、4）**

通过。TCP 层掐断被当成可恢复中断，换链后续传，终态字节与 stub 下发的明文头+身子完全一致。

---

## 场景 3：`cdn_url` 换成 `http://169.254.169.254/latest/meta-data/`

**注入是否生效**

- stub `/direct` 的 JSON 里 `payload_cdn_host=169.254.169.254`（stub 自己记了一份 host，响应体给了客户端）。
- `direct` 调用 1 次；**CDN 请求计数 = 0**（没有任何 Range GET 离开本机去元数据，也没有打到 stub 的 `/cdn/`）。
- 预检：`validate_url_safe("http://169.254.169.254/latest/meta-data/")` 抛 `Access to cloud metadata endpoint is blocked`；`127.0.0.1` 因白名单放行。两相对照，说明白名单没有把元数据一起放掉。

**观察到的行为**

- `elapsed_s = 0.0039`（远小于 10s 连接超时）。
- `DownloadFailedError: wechat direct CDN failed SSRF check sph_code=SPHSSRF001 offset=20`。
- 无落盘文件。日志 ERROR 行只含 sph_code/offset，不含元数据 URL 字面量。

**判定（不变式 6 的「初始 cdn_url」部分）**

通过。直链指向元数据时，校验在 `requests.get` 之前生效，fail fast。与场景 1b 的「先连再失败」可区分：这里是路径走到了校验且拦住，不是「没走到 CDN 分支」。

---

## 场景 4：`head_b64` 解码后无 `ftyp` magic

**注入是否生效**

- stub 返回 `head_b64` = 20 字节且 `[4:8] != b"ftyp"`（`XXXX`），`encrypted_head_bytes=20` 与解码长度一致（通过 client 的长度校验，专门打 downloader 的 magic 校验）。
- `/direct` 1 次且 HTTP 200；**CDN 计数 = 0**。

**观察到的行为**

- `DownloadFailedError: wechat direct head lacks ftyp magic sph_code=SPHNOFTYP1 head_bytes=20`。
- 无落盘文件（进入 `_decode_wechat_head` 失败发生在 `create_temp_file` 之后、写 CDN 之前；异常路径 `_discard_temp`）。
- 没有对 CDN 的 Range 请求，也没有「先写坏头再继续」。

**判定（不变式 3、4）**

通过。无 ftyp 时 fail fast，不静默产出坏文件。

---

## 场景 5：密钥隔离（全程 stub 记 headers）

**注入是否生效**

- resolver 与 CDN 都是同一 HTTP 进程，每个请求的 header 名与值都记下来。场景 2（完整成功路径，含换链）是主证据；1/3/4 作对照。

**观察到的行为（场景 2）**

| 角色 | path | `X-API-Key` | `Range` |
|------|------|-------------|---------|
| resolver | `/api/stream/wechat_channels/SPHABORT50/direct` ×2 | **有**，值等于配置钥 | 无 |
| CDN | `/cdn/token-R2CDNTOKENAAAA1111.mp4` | **无**；头集合 = Host, User-Agent, Accept-Encoding, Accept, Connection, Range | `bytes=20-` |
| CDN | `/cdn/token-R2CDNTOKENBBBB2222.mp4` | **无**（同上） | `bytes=16404-` |

场景 1 修复后：302 与 redirect-target 两次 CDN GET 同样无 `X-API-Key`，Range 被跟随转发。场景 3/4 无 CDN 请求；其唯一的 `/direct` 带钥。

**判定（不变式 2）**

通过。`all_resolver_have_key=true`，`any_cdn_has_key=false`。

---

## 场景 6：日志不含 stub 下发的 `cdn_url` 字面量

**注入是否生效**

- 故意使用不会在正常日志模板里出现的 token：`R2CDNTOKENAAAA1111` / `R2CDNTOKENBBBB2222`。
- loguru sink 开到 DEBUG，捕获 downloader / client / `url_validator` 的真实输出；异常 `str(e)` 一并扫描。

**观察到的行为**

- **INFO / WARNING / ERROR**（downloader 与 client 自己打的行）只含 `sph_code` / `offset` / `content_length` / `status` / `gained` / 异常类名。场景 2 成功路径与场景 3/4 失败路径的异常消息均不含 token。
- **DEBUG**：`url_validator._validate_and_resolve` 存量代码 `URL safety check passed: {url[:100]}` 会把完整 `cdn_url`（含 token）打出来。场景 2 捕获到两条 DEBUG，分别含 A/B 两个 token。生产 `config.jsonc` 与 example 的 `log.level` 均为 **INFO**，默认不会落这条。
- 这是新代码把 `cdn_url` 传进存量 `validate_url_safe` 之后的副作用，不是 downloader 自己把直链写进 INFO。

**判定（不变式 1）**

INFO/异常层通过。DEBUG 层记 P3（见 Findings），不阻塞。不把存量 `url_validator` 本身当本 diff 的缺陷。

---

## 路由隔离（补充探针，非卡面六场景之一）

对真实构造出的 `MediaResolverDownloader._wechat_stream_sph_code`：

| URL | 是否走新路径 |
|-----|----------------|
| `http://127.0.0.1:<port>/api/stream/wechat_channels/SPHROUTE01`（netloc 匹配 + path 正则命中） | 是，`sph_code=SPHROUTE01` |
| 同上但 path 多 `/direct` 后缀 | 否 |
| `http://example.invalid/api/stream/wechat_channels/SPHROUTE01`（netloc 不同） | 否 |

与不变式 5 一致。

---

## 跨文档一致性核对

对照对象：

- 本 diff：`docs/guides/media_resolver.md`（相对 `4e0a5ab` 的 12 行改动）
- 上游：`~/projects/work/MediaResolverAPI/README.md`「GET /api/stream/wechat_channels/{sph_code}/direct」与「客户端拼接协议」

| 上游条款 | 下游文档 | 实现（本轮或 R1） | 出入 |
|----------|----------|-------------------|------|
| `GET .../direct` 扁平 JSON：`sph_code, cdn_url, content_length, encrypted_head_bytes, head_b64, content_type` | 写了端点与「解密文件头 + CDN 直链」，未列字段表，未提 `content_type` | client 校验前 5 项；`content_type` 忽略 | 省略，不构成协议冲突 |
| `cdn_url` 含时效 token，**不得写入日志或持久化** | 只写「CDN 直链含时效 token」，未复述禁日志 | INFO 层做到；DEBUG 见 P3 | 下游文档弱于上游契约 |
| `encrypted_head_bytes >= content_length` 则不要再发 Range | 未写 head-only 分支 | 实现有该分支 | 文档省略 |
| Range 起点 = `encrypted_head_bytes`（示例 `bytes=131072-`） | 只写「从已下载偏移续传（最多 5 次）」 | 实测首次 `Range: bytes=20-`（本 stub 头长 20），续传 `bytes=16404-` | 实现与上游一致；下游未写初始偏移 |
| 连接被掐或 401/403/404/410 → 换链续传 | 只写「慢读可能被 CDN 掐断」 | 掐断实测通过；4xx 集合在代码里，本轮未再打 4xx | 下游未提 4xx 换链 |
| 收尾字节数 == `content_length`，不等则续传或重拉 | 未写收尾校验 | 实现是校验失败 **抛错**（有界失败），不是无限重拉 | 与本仓不变式 4 一致，比上游「重拉」更严；文档未写 |
| `/direct` 不占流式并发槽；429 是流式端点的事 | 本 diff **删掉**了 429 用户提示行，401 排查改为「确认 `/direct` 带钥」 | 与上游分工一致 | 一致 |
| 流式 `video_url` 仍指向 `/api/stream/wechat_channels/{sph}` | 明确写「仍指向流式端点路径，下载器识别后改调 `/direct`」 | 路由探针确认 | 一致 |
| 只有调 `/direct` 带 `X-API-Key`，CDN 不带 | 写了 | 场景 5 实测 | 一致 |
| 「安全」节：SSRF 作用于 MediaResolverAPI 返回的「视频直链」 | 沿用旧表述（历史上指 `video_url`） | 新路径 SSRF 打在 `cdn_url`；`video_url` 是 resolver 流式端点 | 文档未点名 `cdn_url`，也未提 302 目标不复核 |

**文档 finding**：无协议写反。有若干省略（head-only、4xx 换链、禁日志、cdn_url SSRF）。记 P3，不阻塞。排查表里「netloc 不一致则 X-API-Key 不携带」描述的是**未命中新路径时**的旧流式下载行为，与 `/direct` 路径不矛盾。

---

## Findings

### P2-1（确认 R1，不升级）CDN GET 跟随 30x，Location 未过 `validate_url_safe`（不变式 6）

- **本轮新证据**：场景 1 修复后，stub 对 token URL 回 302、对 `/cdn/redirect-target` 回 206，客户端跟随并把目标正文拼成完整 mp4（`equals_full=true`）。场景 1b 把 Location 设为 `http://169.254.169.254/latest/meta-data/`，耗时 20.0s（两次 10s connect timeout），证明对元数据 IP **发起了真实 TCP**；对比场景 3 直链元数据 0.0039s 即被拦。
- **工具标注 / 本仓判定 / 两问**：R1 标 P2。本仓仍 P2。
  1. 真实使用下会被触发吗？本机三次 curl 元数据均超时，无 IMDS。正常微信 CDN 对带 Range 的 GET 回 206/4xx，不 302 到内网。要走到 1b 这种洞，需要「已通过 SSRF 的 URL」（例如攻击者控制的公网主机，或异常 CDN）再 302 到内网。自有 resolver + 微信 CDN 的主路径不会这样。第一问在本机量过：元数据不可达，正常 CDN 行为未在本轮观察（无真实 token，按卡面用 stub 注入）。
  2. 触发了后果能否接受？跟随后的响应体写入临时 mp4，成功则进转录；CDN 请求不带 `X-API-Key`（场景 5）。本机无 IMDS，1b 的后果是 `no progress` 失败、不落盘。云上若开放 IMDS，理论上可把元数据拼进文件——这是防线缺口，但当前真实部署测不到该后果。internal 的「越权访问」两问未同时成立 → **不升 P1**。
- **建议（与 R1 相同，不重复开新意见）**：CDN GET 加 `allow_redirects=False`；30x 落入既有 `status != 206` 换链逻辑。

### P3-1 DEBUG 日志可含 `cdn_url`（不变式 1 边缘；不审存量实现）

- 新代码把带 token 的 `cdn_url` 传给存量 `validate_url_safe`。后者 DEBUG 行 `URL safety check passed: {url[:100]}` 会打出 token。生产 `log.level=INFO`，INFO/异常层实测干净。记 backlog：若有人把级别调到 DEBUG，时效 token 会进日志文件。修复应在新代码侧避免把完整 `cdn_url` 交给会记日志的 API，或对 validator 的 debug 做截断/脱敏——那是后续卡，本轮不改被审代码。

### P3-2 下游文档省略上游协议若干条款（跨文档）

- 未复述「cdn_url 不得进日志」、head-only 不再 Range、401/403/404/410 换链、收尾长度校验、SSRF 对象是 `cdn_url`。没有写反。不阻塞。

R1 的 P2-2（`test_final_size_mismatch_raises` 名不符实）与其余 P3 本轮未再测，不重复、不撤销。

## 降层三问（本轮运行时答案）

1. **终态交付前哪些动作不可逆？** 场景 3/4/1b 失败均无残留文件。场景 2 成功才返回路径。网络侧全是 GET。无通知、无对外写入。
2. **续传 offset 的事实源？** 场景 2：stub 广告 32768、只给 16384 → 客户端 `gained=16384` → 下一跳 `Range: bytes=16404-`。offset 来自实际写入字节，不是 Content-Range 反推。
3. **保护的是写入计数还是产出文件？** 场景 2 断言打在落盘字节与期望 `FULL` 逐字节相等，不是只比长度计数器。

## 总结论

**pass**。无新增 P1；必修清单为空。

本轮用真实 HTTP 对抗跑完卡面 6 场景：密钥隔离、掐断续传完整性、直链 SSRF、无 ftyp fail fast、INFO 层 token 保密均成立。R1 P2-1（302 跟随绕过 SSRF）被运行时**确认**（含跟随后写文件、以及 302 到 169.254.169.254 的真实 TCP），两问后仍为 P2，不阻塞合并。P3 两条（DEBUG 泄露、文档省略）记 backlog。
