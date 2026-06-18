#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性脚本：抓取小宇宙(xiaoyuzhoufm)某个节目的全部历史单集。

参考 ultrazg/xyz 项目的 API 文档（/v1/search/create、/v1/episode/list、
/app_auth_tokens.refresh），直接调用小宇宙官方 API。

设计要点：
- 敏感信息(access/refresh token)全部通过环境变量读取，禁止硬编码，避免提交到开源仓库。
- 通过节目名搜索拿到 pid，再用 loadMoreKey 分页拉全部单集。
- 输出写入 data/output/ （已被 .gitignore 忽略），不污染仓库。
- 所有 console 日志为纯英文（遵循项目规范）。

环境变量：
- XYZ_ACCESS_TOKEN   (必填) x-jike-access-token；刷新接口也要求带上它（即便已过期）
- XYZ_REFRESH_TOKEN  (可选) x-jike-refresh-token，提供后可在 401 时自动刷新重试
- XYZ_DEVICE_ID      (可选) x-jike-device-id，部分情况下网关会校验
- XYZ_PODCAST_NAME   (可选) 节目名，默认 "肥话连篇"
- XYZ_PID            (可选) 直接指定节目 pid，跳过搜索

用法：
  # 在项目根目录的 .env 写入 XYZ_ACCESS_TOKEN=... (该文件已被 gitignore)
  uv run python scripts/fetch_xiaoyuzhou_episodes.py
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
BASE_URL = "https://api.xiaoyuzhoufm.com"
SEARCH_PATH = "/v1/search/create"
EPISODE_LIST_PATH = "/v1/episode/list"
REFRESH_PATH = "/app_auth_tokens.refresh"

DEFAULT_PODCAST_NAME = "肥话连篇"

# 输出目录：data/output 已在 .gitignore 中，敏感结果不会被提交
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "data" / "output" / "xiaoyuzhou"

REQUEST_TIMEOUT = 20  # seconds
PAGE_SLEEP = 0.4      # 翻页间隔，做个温和的限速，避免触发风控


# ----------------------------------------------------------------------------
# 环境变量 / .env 加载
# ----------------------------------------------------------------------------
def load_dotenv_if_present() -> None:
    """尽量加载项目根目录下的 .env（优先 python-dotenv，缺失则用极简解析器）。"""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path)
        return
    except Exception:
        pass
    # 极简兜底解析：KEY=VALUE，忽略注释与空行
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# 运行期可变状态（刷新后会被替换）
_ACCESS_TOKEN = ""
_REFRESH_TOKEN = ""
_DEVICE_ID = ""

# device-id 持久化文件（gitignore 的 data/ 下），保证多次运行复用同一 device-id
TOKEN_STATE_FILE = PROJECT_ROOT / "data" / "output" / "xiaoyuzhou" / ".token_state.json"


def _gen_device_id() -> str:
    """生成 UUID 形式的随机 device-id（8-4-4-4-12 十六进制）。"""
    import random
    chars = "0123456789abcdef"
    segs = [8, 4, 4, 4, 12]
    return "-".join(
        "".join(random.choice(chars) for _ in range(n)) for n in segs
    )


def init_identity() -> None:
    """初始化 access/refresh token 与 device-id：状态文件 > 环境变量 > 自动生成。"""
    global _ACCESS_TOKEN, _REFRESH_TOKEN, _DEVICE_ID
    state = {}
    if TOKEN_STATE_FILE.exists():
        try:
            state = json.loads(TOKEN_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    _ACCESS_TOKEN = (state.get("accessToken")
                     or os.environ.get("XYZ_ACCESS_TOKEN", "").strip())
    _REFRESH_TOKEN = (state.get("refreshToken")
                      or os.environ.get("XYZ_REFRESH_TOKEN", "").strip())
    _DEVICE_ID = (os.environ.get("XYZ_DEVICE_ID", "").strip()
                  or state.get("deviceId") or _gen_device_id())


def save_identity() -> None:
    """持久化当前 token 与 device-id，便于下次复用、避免重复轮换。"""
    try:
        TOKEN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STATE_FILE.write_text(json.dumps({
            "accessToken": _ACCESS_TOKEN,
            "refreshToken": _REFRESH_TOKEN,
            "deviceId": _DEVICE_ID,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[token] warn: save state failed: {exc}")


def build_headers(extra: dict | None = None) -> dict:
    """构造模拟小宇宙 Android 客户端的请求头（配方取自实测可用项目）。"""
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "Xiaoyuzhou/2.102.2(android 36)",
        "os": "android",
        "os-version": "36",
        "manufacturer": "Xiaomi",
        "model": "23127PN0CC",
        "applicationid": "app.podcast.cosmos",
        "app-version": "2.102.2",
        "app-buildno": "1395",
        "x-jike-device-id": _DEVICE_ID,
        "x-jike-access-token": _ACCESS_TOKEN,
    }
    if extra:
        headers.update(extra)
    return headers


# ----------------------------------------------------------------------------
# Token 刷新
# ----------------------------------------------------------------------------
def refresh_access_token() -> bool:
    """用 refresh token 换取新的 access token。

    实测可用配方（来自 online-video-history-server）：仅需 device-id + refresh-token，
    Content-Type 为 JSON，body 为 {}，**不带** access-token 头。
    注意：refresh token 是一次性轮换的，刷新成功后旧 token 立即失效，
    新的 refresh token 会被持久化，避免与其他服务共用同一 token 造成互相失效。
    """
    global _ACCESS_TOKEN, _REFRESH_TOKEN
    if not _REFRESH_TOKEN:
        print("[token] no refresh token available, cannot refresh")
        return False

    print("[token] refreshing access token via refresh token ...")
    headers = {
        "Content-Type": "application/json;charset=utf-8",
        "User-Agent": "Xiaoyuzhou/2.102.2(android 36)",
        "x-jike-device-id": _DEVICE_ID,
        "x-jike-refresh-token": _REFRESH_TOKEN,
    }
    resp = requests.post(
        BASE_URL + REFRESH_PATH, headers=headers, json={}, timeout=REQUEST_TIMEOUT
    )
    if resp.status_code != 200:
        print(f"[token] refresh failed: status={resp.status_code} "
              f"body={resp.text[:160]}")
        return False

    try:
        body = resp.json()
    except ValueError:
        print("[token] refresh returned non-JSON body")
        return False

    new_token = body.get("x-jike-access-token", "")
    new_refresh = body.get("x-jike-refresh-token", "")
    if new_token:
        _ACCESS_TOKEN = new_token
        if new_refresh:
            _REFRESH_TOKEN = new_refresh  # 轮换后的新 refresh token
        save_identity()
        print("[token] refreshed successfully (state persisted)")
        return True
    print("[token] refresh 200 but no access token in response")
    return False


def post_json(path: str, body: dict) -> dict:
    """POST 一个 JSON 请求；遇 401 且有 refresh token 时自动刷新并重试一次。"""
    url = BASE_URL + path
    for attempt in range(2):
        resp = requests.post(
            url, headers=build_headers(), json=body, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 401 and attempt == 0 and refresh_access_token():
            continue
        if resp.status_code != 200:
            raise RuntimeError(
                f"POST {path} failed: status={resp.status_code}, "
                f"body={resp.text[:300]}"
            )
        return resp.json()
    raise RuntimeError(f"POST {path} failed after token refresh")


def unwrap(obj: dict) -> dict:
    """兼容直连官方 API 与 xyz 代理两种返回结构，统一取到真正的数据层。"""
    # xyz 代理会包一层 {code,msg,data:{...}}；官方直连则直接是 {data:[...],...}
    if isinstance(obj.get("data"), dict):
        return obj["data"]
    return obj


# ----------------------------------------------------------------------------
# 业务逻辑
# ----------------------------------------------------------------------------
def search_podcast_pid(name: str) -> str:
    """按节目名搜索，返回最匹配节目的 pid。优先精确标题匹配，否则取第一个候选。"""
    print(f"[search] querying podcast by name: {name!r}")
    payload = unwrap(post_json(SEARCH_PATH, {"keyword": name, "type": "PODCAST"}))
    items = payload.get("data", []) or []
    podcasts = [it for it in items if it.get("type") == "PODCAST"]
    if not podcasts:
        raise RuntimeError("no podcast found for the given name")

    # 打印候选，便于人工核对
    for idx, pc in enumerate(podcasts):
        print(
            f"[search] candidate[{idx}] pid={pc.get('pid')} "
            f"title={pc.get('title')!r} author={pc.get('author')!r}"
        )

    exact = [pc for pc in podcasts if pc.get("title") == name]
    chosen = exact[0] if exact else podcasts[0]
    if not exact:
        print("[search] no exact title match, using first candidate")
    print(f"[search] chosen pid={chosen.get('pid')} title={chosen.get('title')!r}")
    return chosen["pid"]


def slim_episode(ep: dict) -> dict:
    """从原始单集对象中提取常用字段，生成精简记录。"""
    enclosure = ep.get("enclosure") or {}
    media = ep.get("media") or {}
    eid = ep.get("eid", "")
    return {
        "eid": eid,
        "title": ep.get("title", ""),
        "pubDate": ep.get("pubDate", ""),
        "duration": ep.get("duration"),  # 秒
        "isPrivateMedia": ep.get("isPrivateMedia", False),
        "audio_url": enclosure.get("url") or media.get("source", {}).get("url", ""),
        "episode_url": f"https://www.xiaoyuzhoufm.com/episode/{eid}" if eid else "",
        "playCount": ep.get("playCount"),
        "commentCount": ep.get("commentCount"),
    }


def fetch_all_episodes(pid: str) -> list[dict]:
    """用 loadMoreKey 分页拉取该节目的全部单集，返回原始单集对象列表。"""
    all_eps: list[dict] = []
    load_more_key = None
    total = None
    page = 0

    while True:
        page += 1
        body: dict = {"pid": pid, "order": "desc"}
        if load_more_key:
            body["loadMoreKey"] = load_more_key

        payload = unwrap(post_json(EPISODE_LIST_PATH, body))
        eps = payload.get("data", []) or []
        all_eps.extend(eps)
        if total is None:
            total = payload.get("total")
        load_more_key = payload.get("loadMoreKey")

        print(
            f"[episodes] page={page} got={len(eps)} "
            f"accumulated={len(all_eps)}"
            + (f"/{total}" if total else "")
        )

        if not load_more_key or not eps:
            break
        time.sleep(PAGE_SLEEP)

    if total is not None and len(all_eps) != total:
        print(
            f"[episodes] WARNING: fetched {len(all_eps)} but server total={total}"
        )
    return all_eps


def safe_filename(name: str) -> str:
    """把节目名转成安全的文件名片段。"""
    keep = [c if c.isalnum() or c in "-_." else "_" for c in name]
    return "".join(keep).strip("_") or "podcast"


def main() -> int:
    load_dotenv_if_present()
    init_identity()

    if not _ACCESS_TOKEN and not _REFRESH_TOKEN:
        print(
            "[fatal] need XYZ_ACCESS_TOKEN or XYZ_REFRESH_TOKEN "
            "(set in shell or .env). Aborting.",
            file=sys.stderr,
        )
        return 2

    print(f"[init] device-id={_DEVICE_ID}")
    # 若无 access token 但有 refresh token，先换取一次
    if not _ACCESS_TOKEN:
        if not refresh_access_token():
            print("[fatal] failed to obtain access token via refresh",
                  file=sys.stderr)
            return 2

    podcast_name = os.environ.get("XYZ_PODCAST_NAME", DEFAULT_PODCAST_NAME).strip()
    pid = os.environ.get("XYZ_PID", "").strip()

    try:
        if not pid:
            pid = search_podcast_pid(podcast_name)
        else:
            print(f"[search] using pid from env: {pid}")

        raw_episodes = fetch_all_episodes(pid)
    except Exception as exc:  # 一次性脚本，统一兜底打印
        print(f"[fatal] {exc}", file=sys.stderr)
        return 1

    slim = [slim_episode(ep) for ep in raw_episodes]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = safe_filename(podcast_name)
    raw_path = OUTPUT_DIR / f"{fname}_{pid}_raw.json"
    slim_path = OUTPUT_DIR / f"{fname}_{pid}_episodes.json"

    raw_path.write_text(
        json.dumps(raw_episodes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    slim_path.write_text(
        json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[done] total episodes: {len(slim)}")
    print(f"[done] raw  -> {raw_path}")
    print(f"[done] slim -> {slim_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
