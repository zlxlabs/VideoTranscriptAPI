# 真实生产夹具：两人访谈「元数据 + 角色」推测通路

本目录是 **twitter 任务 2115809881399922849** 的真实 FunASR 转录产物与任务
元数据，用作两人访谈推测命名通路的 producer fixture（跨发布边界验收：LLM
输出要按新 schema 形状构造，转录侧必须是真实产物而不是手编样本）。

## 来源

- 转录：`collison_transcript_funasr.json` —— 生产 FunASR 原样产物（原档 167 段、
  两人 84/83 段、duration 3152.04s）。原档归档在仓外
  `~/.local/state/delegate/cards/fixtures-vta-261030/`，仓内只保留裁剪版。
- 元数据：`metadata.json` —— 该任务的标题、作者、描述，逐字取自生产任务元数据
  （作者 John Collison；标题含唯一一处 `@sama` 提及）。

对话内容是公开发布的播客/语音专场录音转录，取样自公开可见的标题与作者署名。

## 裁剪方式

- **段数**：取原档时间轴**前 40 段**（Speaker1 20 段 / Speaker2 20 段），覆盖
  0.15s–947.23s。原档两人段数 84/83（≈1:1），裁剪后仍是 1:1，因此两位主要
  说话人的发言占比各约 50%——这条比例是通路前提之一（各 ≥15%、两人合计
  ≥80%），裁剪不得改变它。
- **字段**：逐段删掉 `words`（词级对齐数组）。说话人推断路径不读该字段，
  删掉只为控制仓内体积；其余字段（start_time / end_time / text / speaker）
  逐字保留。
- **顶层字段**：`task_id` / `file_name` / `file_hash` / `duration` / `created_at` /
  `processing_time` / `metadata` 全部沿用原档取值。`duration` 是源媒体时长
  （3152s），不是裁剪后片段的跨度。

## 这份夹具锁住的行为

生产现行裁决（`collison_speaker_mapping_prod.json`，仓外留存，仅供对照）把两位
说话人都判成「说话人N 待确认」：作者名 John Collison 被误标成 title_credit 后
判 quote_missing，标题里的 @sama 核对通过却因缺对话佐证被拒。

本卡开放的通路在这份夹具上应当得到：

- Speaker1 → `说话人1｜作者关联推测:John Collison`（角色 host）
- Speaker2 → `说话人2｜标题关联推测:sama`（角色 guest）

两人都不带「待确认」徽章。
