# TextSegmenter 两个 P1 的根因与修法对齐（#146 / #147 的 triage 结论）

- 会话：2026-10-02 pi-lead（glm-5.3）
- 基线：`origin/main` `7e9c3079`
- 触发：用户问「本仓 issues 怎么处理合适」，要求先第一性原理 + 咨询 codex / claude-opus-5-5 / kimi
- 本文档是决策真身。执行器质疑期望值时先对照这里，不要在 PR 里重新讨论。

## 0. 结论摘要

收件箱当时只剩 #146、#147 两张标着 P2、写着「需要先做产品决策再派卡」的单。
三家顾问独立给出的形状判断一致：**这两张单的定性是错的**——它们各自盖住了一条 P1。
本轮 triage 后拆成 5 张卡，本文档只钉住第 1 张（两个 P1）的边界与验收。

| 卡 | 内容 | 档位 | 状态 |
|---|---|---|---|
| 卡 1 | `TextSegmenter` 挂死 + 正文改写 | **P1** | 本文档；下一节起 |
| 卡 2 | `validate_config` 长度/阈值字段取值校验 | P2 | 未派 |
| 卡 3 | `DialogSegmenter` 尾块合并不复查 `max_chunk_length` | P2 | 未派 |
| 卡 4 | #146 清单重写 + `dialog_segmenter.py:338` 补 `!?` | P2 | 未派 |
| 卡 5 | #147 剩余部分（`from_dict` 重复字面量去重 + 示例配置注释/键集合锁） | P2 | 未派 |

卡 2 与卡 1 有交集（都碰长度配置的合法性），但**不是同一件事**：卡 1 修的是「非法值造成什么后果」，
卡 2 修的是「非法值从哪里被挡住」。两者都做，任一缺失另一张仍成立。

## 1. P1-A：`_append_fragment` 在 `max_segment_size <= 0` 时死循环

### 实测（主脑亲跑，`origin/main` `7e9c3079`）

```sh
PYTHONPATH=src timeout 10 uv run --frozen python -c "
from video_transcript_api.llm.core.config import LLMConfig as C
from video_transcript_api.llm.segmenters.text_segmenter import TextSegmenter as T
print(T(C(api_key='k',base_url='u',calibrate_model='m',summary_model='m',max_segment_size=0)).segment('你好。'*10))"
echo $?   # → 124（被 timeout 杀掉，即挂死）
```

机理（`src/video_transcript_api/llm/segmenters/text_segmenter.py:96-121`）：

```python
while fragment:
    available = self.max_segment_size - len(current_segment)   # 0 - 0 = 0
    if available <= 0:
        ... current_segment = ""; available = self.max_segment_size   # 仍是 0
    take = min(len(fragment), available)                     # min(n, 0) = 0
    current_segment += fragment[:take]                       # 加空串
    fragment = fragment[take:]                               # fragment 原样不变 ← 循环变量不单调
    if len(current_segment) >= self.max_segment_size:       # 0 >= 0 恒真
        segments.append(current_segment.strip())            # 持续 append 空串
```

`fragment` 在所有可达输入下都不缩短，`while` 永不退出，且 `segments` 列表无界增长。
负数情形推演同样不单调：余料短于 `|neg|` 后 `fragment[-k:]` 不再变短。

### 为什么这是 #142 的漏网而不是新问题

#142（`7e9c3079` 合并）抽出 `src/video_transcript_api/utils/text_split.py`，
`split_oversized_text` 对 `max_len <= 0` fail fast，并接到**两个**执法点：
`capswriter_client._split_long_segment` 与 `dialog_segmenter._split_long_dialog`。

`TextSegmenter._append_fragment` 是**第三个**长度执法点，它自有一个 `while fragment:`，
从未接入 `split_oversized_text`，因此那条 fail fast 覆盖不到它。
三处顾问（codex / claude-opus / kimi）都独立发现了这个漏网点，主脑逐条复核后确认成立。

### 触发路径（生产上是活的）

`coordinator.py:100` 构造 `PlainTextProcessor` → `plain_text_processor.py:49` 持有 `TextSegmenter`
→ 文本长度超 `enable_threshold` 时走 `plain_text_processor.py:124` 的 `self.segmenter.segment(text)`。
`max_segment_size` 来自 `config/config.jsonc` 的 `llm.segmentation.max_segment_size`，
该文件是**未跟踪、手工维护、只存在于生产机**的文件，本仓对它**零取值校验**。
owner 写一个 `0` 或负数即可挂死 worker——这与 #142 第一轮那个已被实证的挂死形态**完全同构**。

### P1 判定（personal 档：数据丢失 / 静默出错 / 崩溃）

- 第一问「真实使用方式下会被触发吗」：会。触发方式已被 #142 实证过一次（owner 手改配置）。
- 第二问「后果能否接受」：进程挂死，不可接受。
- 两问都过 → **P1**。

## 2. P1-B：`_segment_by_sentences` 改写用户正文

### 实测（主脑亲跑）

```sh
PYTHONPATH=src uv run --frozen python -c "
from video_transcript_api.llm.core.config import LLMConfig as C
from video_transcript_api.llm.segmenters.text_segmenter import TextSegmenter as T
print(repr(T(C(api_key='k',base_url='u',calibrate_model='m',summary_model='m'))._segment_by_sentences('圆周率是3.14！见https://a.b/c。')))"
# → ['圆周率是3。14。见https。//a。b/c。']
```

输入 `圆周率是3.14！见https://a.b/c。`，输出 `圆周率是3。14。见https。//a。b/c。`。
小数点被替换成句号、URL 被拦腰截断、`！` 被抹成 `。`。

### 机理（`text_segmenter.py:78-93`）

```python
sentences = re.split(r'[。！？!?\.…，,；;：:\n]', content)   # 无捕获组 → 全部原标点被丢弃
...
sentence = sentence.strip()
fragment = sentence + "。"                                  # 每片补一个句号
```

两处叠加：**丢弃**全部原标点（不可逆的信息丢失）+ **注入**一个 `。`（改变语义）。
`3.14` 被切成 `3.` 与 `14`，再各自补 `。`。

### 为什么它会变成最终产物（这是它够 P1 的关键）

`plain_text_processor.py` 的校对流程把 `TextSegmenter` 的输出直接当 `transcript` 送进 LLM prompt
（`plain_text_processor.py:241`）。校对成功时用的是 LLM 输出；**校对降级时**走
`_fallback_plain_text(segment, ...)`（调用点 `:326` / `:360` / `:393`，实现 `:459-483`），
它的 `original` 形参收到的就是**这份已被改写的 segment**，于是原样返回。

生产实际取到的策略是 `formatted_original`：主脑实测
`LLMConfig.from_dict({'llm': {...}}).structured_fallback_strategy == 'formatted_original'`，
而 dataclass 默认是 `'best_quality'`（详见 §4）。也就是说生产走的正是「原样返回改写稿」那条路。

### P1 判定

- 第一问：会。触发形态是普通的中英混说转录里出现小数或网址，不需要任何异常配置。
- 第二问：最终交付给用户的正文里出现 `3。14`、`https。//a。b/c`，属静默出错，不可接受。
- 两问都过 → **P1**。

## 3. 卡 1 的锁定决策

### 3.1 修法（两处，执行器不得另选形状）

**A. `_append_fragment` 的挂死**：在 `TextSegmenter.__init__` 对 `max_segment_size <= 0` **fail fast**，
抛 `ValueError`，错误串必须含可 grep 的 `TEXT_SEGMENT_INVALID_MAX_SIZE` 与 `condition=max_len_not_positive`。
- 为什么不 clamp：clamp 会把配置错误静默纠正成另一个宽度，正是「静默出错」那一档（与 #142 同一判断）。
- 为什么不放循环里、为什么不在 `_append_fragment` 里：放 `__init__` 是最早的可诊断位置，
  栈更浅，且一次性覆盖 `segment()` 的两条分支（行式与句式）。
- `segment_size <= 0` **不校验**，并在代码注释里写明理由：它只参与
  `if len(current_segment) >= self.segment_size` 的落盘判断，不参与循环终止条件，不会挂死。

**B. `_segment_by_sentences` 的正文改写**：改成**只切不换**。
- split 正则加捕获组保留原标点，断点集合**去掉 ASCII `.`**（现状 `[。！？!?\.…，,；;：:\n]` → `[。！？!?…，,；;：:\n]`）。
- 删掉 `fragment = sentence + "。"` 这一行，不再注入句号。
- 去掉 `.` 的理由：`3.14` / `v1.2.3` / `e.g.` / URL 在 `.` 处断开会撕裂正文；
  且本仓另外三处（`capswriter_client.py:181`、`dialog_segmenter.py:338`、`paragraphize.py:21`）
  **都不认 ASCII `.`**，去掉之后本仓四处口径一致。
- 长度不会被放弃：`_append_fragment` 按 `max_segment_size` 逐字符拼装，长度上限由它保证，
  英文无句点的长文本同样被按长度切开。正文守恒是本修法要保住的不变式。

### 3.2 明确不改的（已否决，执行器不得顺手做）

- **不改** `plain_text_processor._merge_into_paragraphs`（`:546-584`）。它也注入 `。`，
  但形态不同：它**只插入不丢弃**，插入位置有 `endswith(('。','！','？','!','?','.',';','；'))`
  白名单保护（`:568`），小数与网址不会被撕裂。降级登记为 P2 backlog，不进本卡。
- **不改** `paragraphize.py` 的任何行为。它的模块契约是「只选边界、不改文本」「长度只是预算不是闸刀」。
- **不建**「统一句末定义」的共享常量或框架。三家顾问一致否掉：`#146` 的完成条件本身是个类别错误
  （详见 §5）。
- **不改** `dialog_segmenter.py:338` 补 `!?`（那是卡 4，会改变分块与时间插值切点，必须单独验）。
- **不改** `plain_text_processor.py` 的任何文件（本卡只读它，不写它）。

### 3.3 验收判据

1. **挂死探针**：`max_segment_size` 取 `0` 与 `-1` 各自让 `TextSegmenter(...)` 抛 `ValueError`，
   错误串含 `TEXT_SEGMENT_INVALID_MAX_SIZE`。
   用例必须用 **10 秒 watchdog 子线程**包住调用——裸调 + `pytest.raises` 在 guard 失效时
   会让整轮测试挂死而不是报错（沿用 #142 那张卡的写法，守护线程不阻塞解释器退出）。
   **反向判据：把该守卫还原成旧实现后，同一条用例必须红，且是断言失败而不是超时。**
2. **正文守恒**：对一组样本（纯中文、中英混说、含小数、含网址、含 `Mr. Smith` / `e.g.`、
   含 `!?`、纯英文无标点、超 `max_segment_size` 的长文本），
   断言「所有输出段拼接后，其非空白字符序列 == 输入的非空白字符序列」。
   这条是性质断言，对更宽的错误实现也拦得住。
3. **具体回归**：`3.14`、`https://a.b/c`、`Mr. Smith`、`e.g.` 在输出里原样出现；
   输出里**不出现** `3。14`（即不再有被注入的句号）。
4. **既有降级不许变**：
   `uv run --frozen pytest -o addopts= -q -p no:warnings tests/unit/test_text_segmenter.py`
   基线 **17 passed**，改动后仍须 17 passed（既有 17 条用例里没有一条断言被注入的 `。`，
   已逐条读过；若确有用例因本修法必须改，先在报告里写明理由再改）。
5. **全量**：`make test` 退出码 0（基线 `3454 passed, 3 skipped`）。
6. **反向红验**：去掉 B 的改动后第 2、3 条必须红（断言失败，非 ImportError/AttributeError）。

### 3.4 部署注意

本卡改运行时逻辑，合并后**需要手工部署到 n305**（本仓未接 D3 自动部署）。
形态见 agent-config `memory/repos/VideoTranscriptAPI/n305-docker-deploy.md`：**不可 scp 服务器 compose**。
部署后容器内复测两条链（走应用同一解释器 `uv run` + `sys.path.insert(0, "/app/src")`，
容器未设 `PYTHONPATH`）：① `max_segment_size=0` 抛 `ValueError`；② `_segment_by_sentences` 不再改写正文。

## 4. triage 时另外确认的三件事（不进本卡，写在这里防重复劳动）

### 4.1 `from_dict` 与 dataclass 默认已经有一处真实分叉

主脑实测：

```sh
PYTHONPATH=src uv run --frozen python -c "
from video_transcript_api.llm.core.config import LLMConfig as C
r=dict(api_key='k',base_url='u',calibrate_model='m',summary_model='m')
print(C.from_dict({'llm':r}).structured_fallback_strategy, C(**r).structured_fallback_strategy, C.max_segment_size)"
# → formatted_original best_quality 3000
```

`structured_fallback_strategy` 的 dataclass 默认是 `best_quality`（`config.py:87`），
`from_dict` 缺键时给出 `formatted_original`（`config.py:183-188`，由 `fallback_to_original` 推导）。
这条**不是** #147 列的 6 个漂移字段之一（那 6 个经主脑逐条核对，两处确实一致），
但它证明 #147 担心的「两处来源会漂移」在本仓**已经发生了一处**，不是假设。
影响面：走 `from_dict` 的生产路径与直接构造的测试路径行为不同。
处置：登记进 #147，不在本卡。

### 4.2 `DialogSegmenter` 尾块合并绕过上限（卡 3）

主脑实测：

```sh
# max=1500 / preferred=800 / min=300，两条对话 1490 + 20
# → chunk lens = [1510] | OVER 1500? True | text conserved? True
```

`dialog_segmenter.py:94` 的 `chunks[-1].extend(current_chunk)` 只看 `min_chunk_length`、
不复查 `max_chunk_length`。后果轻微（超 0.7%），但它证明 #142 的「上限不可放弃」不变式
**目前只覆盖两条链**。codex 独立发现同一条。处置：卡 3。

### 4.3 #146 的清单不全，且引用的行号已漂移

issue #146 的表列 4 处。主脑与三家顾问各自复核后确认至少还有：
`dialog_segmenter.py:338`（issue 写的 `:297` 现在是 `_format_dialog_timestamp` 的函数体区域，
是 #142 插入约 90 行兜底代码后漂移出来的旧行号）、
`plain_text_processor.py:568`、`plain_text_processor.py:603`、
`dialog_renderer.py:167`、`text_segmenter.py:44`（标点密度启发式）。

## 5. #146 的完成条件被否掉的理由（写在这里，防止后来人重提）

三家顾问（codex / claude-opus-5-5 / kimi）**独立地**得出同一判断：

- 「哪些字符算句末」不是一个能被共享的单一问题。这几处的**职责**不同：
  `capswriter_client` 要产出字符下标回查 token 坐标、契约是逐字不动；
  `paragraphize` 只在成员边界授权、契约是「长度只是预算不是闸刀」；
  `dialog_segmenter` 控制单次 LLM 调用体积；
  `text_segmenter` **改写正文**、与前三处的「不改文本」契约互斥；
  `dialog_renderer` 是展示层。
- 即便只看字符集，「同一批字符」也只是**当前需求的交集**，不是不变式。
  `paragraphize` 认 `…` 而其余不认，已经示范了合法分叉。
- 建共享常量的收益只是让 5 个字符不再重复；它防的漂移已被「行为锁测试」覆盖，
  反而会把「未来的合法分叉」变成「先重构再改需求」的摩擦力。
- 「一张表 + 一致性测试」若锁的是**源码字符串是否等于表格**，本质只是一个改动检测器，
  没有能抓到真实缺陷的判据；要锁就锁**行为**。

**因此 #146 的完成条件改为**：留一张按「用途 / 识别字符 / 是否改正文 / 是否保留坐标」列出的行为表，
每处就地一行注释指向该表并写明为何不同；测试探**行为**（规范输入的切分结果、改写样本的输出），
不探源码字符串。英文 `.` 的识别规则单独登记为**待产品决策项**，保持 open。

## 6. 用户已拍板的三件事（2026-10-02）

1. **先修两个 P1**（本卡），P2 卡按序排队。
2. **「拒绝未知配置键」先不做**，登记进 #147。理由：允许键清单要维护，
   且生产 `config/config.jsonc` 的内容未知，仓里若有历史过时键会导致上线后开不了机。
3. **英文句号 `.` 保持 open，标注待产品决策**。理由：长度后果已被 #142 的硬切兜底，
   剩下是纯质量问题；小数 / 缩写 / URL 的误切需要真实样本定期望。