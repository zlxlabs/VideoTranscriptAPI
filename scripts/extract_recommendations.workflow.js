export const meta = {
  name: 'extract-feihua-recommendations',
  description: '从《肥话连篇》全部校对转录稿提取主播的实地/好物/影视剧推荐为结构化JSON',
  phases: [{ title: 'Extract', detail: '每集一个Sonnet agent：自检跳过→读稿→提取→写JSON' }],
}

// args: { baseDir, vols? }  vols 不传则默认 1..234（repair 时可传子集）
let a = args
if (typeof a === 'string') { try { a = JSON.parse(a) } catch (e) { a = {} } }
if (!a || typeof a !== 'object') a = {}
const BASE = a.baseDir || '/home/zlx/projects/personal/VideoTranscriptAPI/data/output/xiaoyuzhou'
const TRANS = BASE + '/transcripts'
const OUT = BASE + '/extracted'
const vols = Array.isArray(a.vols) && a.vols.length ? a.vols
  : Array.from({ length: 234 }, (_, i) => i + 1)

const pad = (v) => String(v).padStart(3, '0')

const RULES = (vol) => {
  const p = pad(vol)
  const out = `${OUT}/${p}.json`
  return `你在处理中文播客《肥话连篇》第 ${vol} 集的"校对转录稿"，提取两位主播（肥杰、惠子）的【推荐内容】为结构化 JSON。

第0步（断点续跑自检）：用 Bash 运行：
  test -f ${out} && python3 -c "import json,sys; json.load(open('${out}')); print('VALID')" 2>/dev/null
如果输出 VALID，说明本集已完成，**直接停止**，只返回一行：vol=${vol} skipped

第1步：用 Bash 运行 \`ls ${TRANS}/${p}_*.txt\` 得到本集转录稿的确切路径，然后用 Read 读取它。
- 文件头部是 front-matter（--- 包裹），含 Title 与 Source（即 source_url）。
- 正文是带说话人标签的口语对话「肥杰：…」「惠子：…」，推荐内容散落在闲聊中、无结构。

要提取的三类（每类是数组，可为空数组）：
1) place（实地/出行）：recommender, name(店名/地点/景点), city(城市或区域，不明填""), category(餐厅/景点/活动/住宿/其他), what(吃了什么或做了什么), verdict, reason, quote, name_corrected
2) product（好物）：recommender, name(产品名), category, why_good, price_hint(无则""), verdict, quote, name_corrected
3) media（影视剧/综艺/书）：recommender, title, type(电影|剧集|综艺|纪录片|书|其他), synopsis(1-2句梗概), why_recommended, verdict, quote, name_corrected

公共规则：
- recommender ∈ {"肥杰","惠子","共同"}：按说话人标签判断；两人共同讨论且都认可填"共同"。
- verdict ∈ {"重点推荐","推荐","一般","避雷"}：避雷=明确不推荐/踩坑。
- 专名纠错：店名/地名/剧名/产品名若是 ASR 音译错字，输出纠正后的标准名并 name_corrected=true；拿不准就保留原文、name_corrected=false。
- quote：必须是转录稿里【逐字出现】的原文片段（≤50字，用于反查防幻觉），不要改写或拼接。
- 只提取"明确的推荐或避雷"——主播有正/负评价倾向的才算；顺嘴一提、纯叙事、无评价的不算。宁缺毋滥。

输出 JSON 结构：
{
  "episode": { "vol": ${vol}, "title": "<=Title>", "eid": "<Source URL 中 /episode/ 后的 id>", "source_url": "<=Source>", "pubDate": "" },
  "place": [...], "product": [...], "media": [...]
}

第2步：用 Write 把 JSON 写到：${out}
- UTF-8，缩进2，中文不要转义成 \\uXXXX。
- 【JSON 合法性，必须遵守】必须是严格合法 JSON。任何字符串值内部若出现引号，一律改用中文引号「」或『』，绝对禁止裸 ASCII 双引号 "。
- 写完后用 Bash 运行 \`python3 -c "import json;json.load(open('${out}'));print('OK')"\` 校验；若报错就修正后重写，直到打印 OK。

最后只返回一行纯文本：vol=${vol} place=<数量> product=<数量> media=<数量>`
}

phase('Extract')
const results = await parallel(vols.map((vol) => () =>
  agent(RULES(vol), { label: `vol${pad(vol)}`, phase: 'Extract', model: 'sonnet' })
))

return results.filter(Boolean)
