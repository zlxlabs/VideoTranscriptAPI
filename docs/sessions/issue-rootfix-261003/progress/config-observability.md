# 配置生效值诊断（#147）

## 阶段

implementing → 已实现并自测（本卡交付，尚未合入主干）

## 本段结论

- 基线复核：`main.py --check-config` 此前跑完全部启动期演练后只
  `print("Configuration OK")`，不输出任何生效值；`context.load_and_validate_config`
  返回校验后的 dict（`api/context.py:565-593`）。两项均属实。
- 生效值来源唯一：`main.py` 新增模块级私有函数 `_llm_effective_values(config)`，
  取值走 `LLMConfig.from_dict(config)`——与 `--start` 同一个解析器，本函数不含
  任何默认字面量，只补 `from_dict` 不携带的 `source` 标签。
- 白名单 9 键（按修正后的设计，非 10）：`enable_threshold` / `segment_size` /
  `max_segment_size`、`min_chunk_length` / `max_chunk_length` /
  `preferred_chunk_length` / `calibration_concurrent_limit`、
  `structured_calibration_for_plain`、`structured_fallback_strategy`。
- 来源标签复刻真实分支：前 8 项在 `from_dict` 里是裸 `dict.get(key, default)`，
  键存在（显式 `null` 也算）→ `config` 且 value 原样为 `null`；缺失 → `default`。
  `structured_fallback_strategy` 是唯一真值派生项（`if not structured_fallback_strategy:`），
  `fallback_to_original` 为假值（`False`/`0`/`""`/`None`）→ `best_quality`，缺省/真值
  → `formatted_original`；`fallback_strategy` 非空真值 → `config`。
- 输出形态：`Configuration OK` 原行保留，随后一行
  `json.dumps({"llm_effective": {...}}, ensure_ascii=False, sort_keys=True)`。
- 示例注释：`config/config.example.jsonc` 分段段的 `>=` 更正为严格大于
  （实现是 `llm/processors/plain_text_processor.py:121` 的 `len(text) > enable_threshold`），
  并补一句 plain 源兜底路由（`structured_calibration_for_plain` 决定结构化逐段还是旧纯文本路径）。
  只改注释文字，未动任何取值。

## 决定与否决

- 决定：诊断函数就地放 `main.py`，不给 `LLMConfig` 加接口（唯一消费者是
  `--check-config` 一个 CLI 分支），不新建 provenance 框架。
- 否决：`json.dumps` 整份配置（含 api_key/webhook/cookie）；统一 dataclass 默认 /
  示例 / 生产三套取值；给 `validate_config` 顺带加取值校验；把「显式 null 一律当默认」
  的简化判定（真实 `.get` 会保留 None，标 default 等于编造）。
- 未越界：`src/video_transcript_api/llm/core/config.py` 只调用未改；生产配置与凭据未触达。

## 下一步

- 本批只跑了相关测试（`test_check_config_effective_values.py`、
  `test_runtime_lifecycle.py`、`test_llm_config_defaults.py`、
  `test_user_manager_strict_config.py`，共 196 passed）。全量 `make test` 由主脑
  单独调度的验证卡或真实 CI 跑，本卡不声称全量绿。
- draft PR 已建，等主脑处理 ready / 合并。