#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic post-processing for the 《肥话连篇》recommendation extraction.

读取 data/output/xiaoyuzhou/extracted/{vol:03d}.json，执行三件确定性工作（不调用 LLM）：
  1) 合法性 / 完整性校验：列出缺失或 JSON 非法的集数（供回炉重跑）。
  2) quote 反查：每条推荐的 quote 必须能在对应转录稿里逐字命中，否则标 quote_unverified=true（防幻觉）。
  3) 聚合：汇总成 recommendations_all.json 与可读的 recommendations_all.md，
     按 类别 -> verdict 分组，标注集数 / 主播 / 是否存疑。

用法:
  python3 scripts/aggregate_recommendations.py            # 校验 + 聚合
  python3 scripts/aggregate_recommendations.py --list-bad # 只打印缺失/非法集数（逗号分隔，便于喂回 workflow）
"""
import json
import os
import re
import sys
import glob

BASE = os.environ.get(
    "BASE", "/home/zlx/projects/personal/VideoTranscriptAPI/data/output/xiaoyuzhou")
EXTRACTED = os.path.join(BASE, "extracted")
TRANS = os.path.join(BASE, "transcripts")
TOTAL = int(os.environ.get("TOTAL", "234"))

CATS = ["place", "product", "media"]
CAT_CN = {"place": "实地/出行推荐", "product": "好物推荐", "media": "影视剧推荐"}
VERDICT_ORDER = {"重点推荐": 0, "推荐": 1, "一般": 2, "避雷": 3}
# 主名字段：各类别用于显示的主标识
NAME_KEY = {"place": "name", "product": "name", "media": "title"}


def transcript_path(vol):
    hits = glob.glob(os.path.join(TRANS, f"{vol:03d}_*.txt"))
    return hits[0] if hits else None


def load_transcript_text(vol, cache):
    if vol in cache:
        return cache[vol]
    p = transcript_path(vol)
    txt = ""
    if p:
        with open(p, encoding="utf-8") as f:
            txt = f.read()
    cache[vol] = txt
    return txt


def norm(s):
    """去掉所有空白，便于宽松反查（ASR 文本里空格/换行可能与 quote 不一致）。"""
    return re.sub(r"\s+", "", s or "")


def quote_hit(quote, text, ntext):
    if not quote:
        return False
    if quote in text:
        return True
    return norm(quote) in ntext


def main():
    list_bad = "--list-bad" in sys.argv

    present = {}
    missing, invalid = [], []
    for vol in range(1, TOTAL + 1):
        fp = os.path.join(EXTRACTED, f"{vol:03d}.json")
        if not os.path.exists(fp):
            missing.append(vol)
            continue
        try:
            present[vol] = json.load(open(fp, encoding="utf-8"))
        except Exception:
            invalid.append(vol)

    bad = sorted(missing + invalid)
    if list_bad:
        print(",".join(str(v) for v in bad))
        return

    print(f"[check] total={TOTAL} present={len(present)} "
          f"missing={len(missing)} invalid={len(invalid)}")
    if missing:
        print(f"  missing vols: {missing}")
    if invalid:
        print(f"  invalid vols: {invalid}")

    # ---- quote 反查 + 聚合 ----
    tcache = {}
    rows = []  # 扁平化所有推荐
    counts = {c: 0 for c in CATS}
    unverified = 0
    by_recommender = {"肥杰": 0, "惠子": 0, "共同": 0, "其他": 0}

    for vol in sorted(present):
        d = present[vol]
        ep = d.get("episode", {})
        title = ep.get("title", "")
        text = load_transcript_text(vol, tcache)
        ntext = norm(text)
        for cat in CATS:
            for item in d.get(cat, []) or []:
                q = item.get("quote", "")
                verified = quote_hit(q, text, ntext)
                if not verified:
                    unverified += 1
                rec = item.get("recommender", "")
                by_recommender[rec if rec in by_recommender else "其他"] += 1
                counts[cat] += 1
                rows.append({
                    "vol": vol,
                    "ep_title": title,
                    "category": cat,
                    "recommender": rec,
                    "verdict": item.get("verdict", ""),
                    "name": item.get(NAME_KEY[cat], ""),
                    "quote_unverified": (not verified),
                    "item": item,
                })

    # ---- 写 JSON ----
    out_json = os.path.join(BASE, "recommendations_all.json")
    payload = {
        "stats": {
            "episodes_with_data": len(present),
            "missing": missing, "invalid": invalid,
            "counts": counts, "total_items": len(rows),
            "quote_unverified": unverified,
            "by_recommender": by_recommender,
        },
        "items": rows,
    }
    json.dump(payload, open(out_json, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---- 写 Markdown 清单 ----
    out_md = os.path.join(BASE, "recommendations_all.md")
    lines = []
    lines.append("# 《肥话连篇》推荐总清单\n")
    lines.append(
        f"- 覆盖集数：{len(present)}/{TOTAL}　推荐总条数：{len(rows)}"
        f"（实地 {counts['place']} / 好物 {counts['product']} / 影视剧 {counts['media']}）")
    lines.append(
        f"- 推荐人分布：肥杰 {by_recommender['肥杰']} / 惠子 {by_recommender['惠子']} "
        f"/ 共同 {by_recommender['共同']}　|　quote 存疑 {unverified} 条（⚠ 标注）\n")

    def fmt(r):
        it = r["item"]
        cat = r["category"]
        flag = " ⚠" if r["quote_unverified"] else ""
        head = f"- **{r['name']}**（VOL.{r['vol']:03d}·{r['recommender']}·{r['verdict']}{flag}）"
        if cat == "place":
            extra = f"　{it.get('city','')}｜{it.get('category','')}｜{it.get('what','')}"
            reason = it.get("reason", "")
        elif cat == "product":
            price = it.get("price_hint", "")
            extra = f"　{it.get('category','')}" + (f"｜{price}" if price else "")
            reason = it.get("why_good", "")
        else:
            extra = f"　{it.get('type','')}｜{it.get('synopsis','')}"
            reason = it.get("why_recommended", "")
        return f"{head}{extra}\n  - 理由：{reason}\n  - 原文：「{it.get('quote','')}」"

    for cat in CATS:
        sub = [r for r in rows if r["category"] == cat]
        lines.append(f"\n## {CAT_CN[cat]}（{len(sub)} 条）\n")
        sub.sort(key=lambda r: (VERDICT_ORDER.get(r["verdict"], 9), r["vol"]))
        cur = None
        for r in sub:
            if r["verdict"] != cur:
                cur = r["verdict"]
                lines.append(f"\n### {cur}\n")
            lines.append(fmt(r))

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[aggregate] items={len(rows)} unverified={unverified}")
    print(f"  -> {out_json}")
    print(f"  -> {out_md}")
    if bad:
        print(f"[!] 仍有 {len(bad)} 集缺失/非法，建议回炉：{bad}")


if __name__ == "__main__":
    main()
