# TODOS

## P2: API 速率限制

**What:** 加入基于 IP/Token 的简单速率限制，防止资源耗尽和暴力破解。

**Why:** 当前任何持有有效 Token 的用户可以无限提交任务，无效 Token 尝试无日志警告。

**Pros:** 安全基础设施，防止资源滥用。

**Cons:** 当前用户量小，优先级不高。

**Context:** 可用 FastAPI 的 slowapi 或自定义中间件。需决定限制粒度（每分钟/每小时）和限制值。

**Effort:** S（人工）→ S（CC）

**Priority:** P2

**Depends on:** 无

---

## P2: 内存缓存 TTL/LRU 限制

**What:** 为下载器内存缓存（generic.py 的 `_cached_video_info`）加入 TTL 或 LRU 限制。

**Why:** 当前内存缓存永不过期，长时间运行可能内存泄漏。

**Pros:** 防止内存泄漏，提高长期稳定性。

**Cons:** 改动小，风险低。

**Context:** 可用 `functools.lru_cache` 或 `cachetools.TTLCache`。建议 TTL=1h，maxsize=1000。

**Effort:** S（人工）→ S（CC）

**Priority:** P2

**Depends on:** 无

---

## P2: 确定性 key_info 专有名词兜底替换

**What:** 在 LLM 校对之后，加一道确定性后处理：对 key_info 的 brands/names，用程序规则扫描校对稿里的近音错写并直接替换（如"微煌/威煌"→"威皇"），作为 LLM 没改对时的兜底。

**Why:** ID 锚点重设计保证 LLM "有机会"应用 key_info，但不保证它"一定"会把近音字纠正过来；专有名词（店名/人名）这类高价值错误仍可能漏。

**Pros:** 专有名词纠错从"靠 LLM 自觉"变成"确定性保证"，高价值错误不再漏。

**Cons:** 误替换风险（正常词被错改）；需维护近音变体生成（拼音编辑距离）+ 词边界规则 + 严格误替换测试覆盖。blast radius 大。

**Context:** 触发源为 VOL.170「威皇小海鲜」案例。本次 ID 锚点 PR 先修结构病（整块回退导致零校对），观察 ID 修复后该类错误残留率，再决定是否引入。建议限定：仅对 brands/names，仅在拼音编辑距离 ≤ 阈值的高置信近音区间替换，且加词边界保护。eng-review 决定推迟（2026-06-18）。

**Effort:** M（人工）→ S（CC）

**Priority:** P2

**Depends on:** ID 锚点校对重设计先落地
