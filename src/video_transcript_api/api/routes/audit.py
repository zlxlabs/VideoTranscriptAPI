"""
审计日志路由模块

提供 API 调用记录查询、历史任务浏览和摘要预览功能。
"""

import asyncio
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..context import (
    get_audit_logger,
    get_cache_manager,
    get_logger,
    get_usage_recorder,
    get_user_manager,
    lazy_resource,
)
from ..services.transcription import TranscribeResponse, verify_token
from ..services.view_token_resolver import ViewTokenResolver
from ...utils.llm_status import SummaryStatus

logger = lazy_resource(get_logger)
audit_logger = lazy_resource(get_audit_logger)
usage_recorder = lazy_resource(get_usage_recorder)

# 延迟导入，避免循环依赖；同时保持模块级引用供测试 mock 使用
user_manager = lazy_resource(get_user_manager)

router = APIRouter(prefix="/api/audit", tags=["audit"])

# Endpoints that create a new task_id and are therefore eligible to establish
# history/summary ownership as a *legacy* fallback -- used both by the
# tenancy condition in get_history() and the ownership check inside
# get_task_summary() below. Must be kept in sync with the literal endpoint
# strings routes/tasks.py passes to audit_logger.log_api_call() in
# transcribe_video()/recalibrate() -- deliberately excludes
# f"/api/task/{task_id}" (get_task_status's progress-query endpoint): polling
# is a designed-for read-only capability (routes/tasks.py::get_task_status)
# and must never grant history ownership, only submission does.
SUBMISSION_ENDPOINTS = ("/api/transcribe", "/api/recalibrate")


def _submission_log_join_condition() -> tuple[str, list]:
    """构造 `LEFT JOIN api_audit_logs a` 的 ON 子句限制条件：只关联提交类
    端点（/api/transcribe、/api/recalibrate）产生的那一行审计记录，不关联
    同一 task_id 下可能存在的、任意多条 GET /api/task/{task_id} 轮询行。

    本地 codex review 第 6 轮 G1：驱动表反转（见 _task_attribution_
    condition 的说明）后，所有查询都以 task_audit_snapshots s 为 FROM 表、
    api_audit_logs a 只是 LEFT JOIN 上去补充展示字段（video_url/
    wechat_webhook/api_key_masked/request_time）或legacy 归属判断。这条
    限制条件必须放进 ON 子句而不是 WHERE：调用方现在都是 `FROM s LEFT JOIN
    a`，如果限制条件写进 WHERE，等价于把 LEFT JOIN 退化成 INNER JOIN——
    提交日志行缺失（写库失败等）的快照会被整体过滤掉，重新引入本函数要
    防止的"任务从历史里消失"。写进 ON 则只影响 a 侧关联到哪一行（至多
    一行——同一 task_id 在提交类端点上全局只会有一条记录，见 create_task
    每次生成全新 task_id 的既有假设），不影响 s 侧行本身的去留，也不会
    像 `a.user_id = ?` 那样在 s 侧行沿用 a 值时引入按用户过滤的副作用。

    Returns:
        (sql_fragment, params)：sql_fragment 拼进
        `ON a.task_id = s.task_id AND {sql_fragment}`；params 是对应的
        绑定参数（SUBMISSION_ENDPOINTS 的值）。
    """
    placeholders = ",".join("?" for _ in SUBMISSION_ENDPOINTS)
    return f"a.endpoint IN ({placeholders})", list(SUBMISSION_ENDPOINTS)


def _task_attribution_condition(user_id: Optional[str]) -> tuple[str, list]:
    """共享的任务归属谓词，供任何以 `task_audit_snapshots s` 为驱动表、
    并按 `_submission_log_join_condition()` LEFT JOIN 了 `api_audit_logs a`
    的查询复用（目前 get_history()、get_filter_options()、
    get_task_summary() 的 _check_ownership 三处）。

    驱动表反转（本地 codex review 第 6 轮 G1）：此前 get_history()/
    get_filter_options() 仍以 api_audit_logs a 为 FROM 表，
    task_audit_snapshots.submitted_by 只是 JOIN 上去的附加判断条件——而
    AuditLogger.log_api_call() 会吞掉所有 SQLite 异常并返回 False，调用方
    （routes/tasks.py）从不检查这个返回值。一旦"提交类端点"那一行审计日志
    写失败，任务依然有完整、正确归属的 task_audit_snapshots 快照（
    submitted_by 由 create_task() 从调用方的内存态 user_id 直接写入，不
    依赖审计日志是否写成功），但因为 a 侧压根没有这一行，以 a 为驱动表的
    查询里根本不存在这一行可供 JOIN——任务永久从提交者的历史/过滤选项里
    消失，repair_task_snapshots 也只补快照、不补（也补不出，因为
    wechat_webhook 等字段从未存过 cache.db）日志行，无法自愈。

    修复：所有三处查询翻转为 `FROM task_audit_snapshots s LEFT JOIN
    api_audit_logs a`，s.submitted_by 成为唯一权威判定，不再要求 a 存在。
    本函数只负责这条归属谓词本身：submitted_by 有值（任务创建时由
    /api/transcribe、/api/recalibrate 写入）就是权威判定，与 a 是否存在
    无关；为 NULL（本 PR 迁移前的存量任务）时退回旧的审计行判断——但
    a 已经在 ON 子句里被 _submission_log_join_condition() 限制到提交类
    端点，这里只需再比较 a.user_id，无需重复写 endpoint 条件。

    Args:
        user_id: 当前调用方的 user_id，用于 submitted_by / a.user_id 比较。

    Returns:
        (sql_fragment, params)：sql_fragment 是一段可直接拼进 WHERE 子句的
        括号表达式；params 是按 sql_fragment 中占位符顺序绑定的参数列表
        （user_id 出现两次，OR 的两个分支各一次），调用方直接原样拼进自己
        的 params 列表即可。
    """
    condition = "(s.submitted_by = ? OR (s.submitted_by IS NULL AND a.user_id = ?))"
    params = [user_id, user_id]
    return condition, params


def _normalize_history_status(status: Optional[str]) -> str:
    """Limit history filters to statuses represented by terminal snapshots."""
    effective_status = status or "success"
    if effective_status not in {"success", "failed", "all"}:
        raise HTTPException(
            status_code=422,
            detail="status must be one of: success, failed, all",
        )
    return effective_status


@router.get("/stats")
async def get_audit_stats(days: int = 30, user_info: dict = Depends(verify_token)):
    """获取 API 调用统计与 LLM token 用量统计（按 days 时间窗口）。

    user_stats 已按 user_id 过滤，天然只反映调用方自己的数据。llm_usage
    则不同：llm_usage 表没有 user_id 列（token 用量按任务/阶段审计，不是
    按用户维度设计的），usage_recorder.get_stats() 聚合的是全库所有调用方
    的用量总和。多用户模式下，若不加区分地把这份全局聚合返回给任意认证
    通过的用户，会把其他租户的调用规模/成本信息泄露给彼此（ci-gate
    review）——因此只对能代表"系统所有者"视角的调用方暴露：
    - 单用户模式（未配置多用户表，只用 fallback token 登录）：压根不存在
      "其他用户"，全局聚合等价于当前唯一用户自己的用量，暴露没有隐私问题。
    - 多用户模式下，只有仍用 legacy fallback token（is_legacy=True，通常是
      部署者留给自己的运维入口）登录才能看到；_users_data 里配置的具体
      租户用户看不到，llm_usage 返回 None。
    """
    try:
        user_id = user_info.get("user_id")
        # SQLite 查询为同步阻塞调用，放到线程池避免阻塞事件循环
        user_stats = await asyncio.to_thread(audit_logger.get_user_stats, user_id, days)

        can_view_global_llm_usage = (
            not user_manager.is_multi_user_mode() or user_info.get("is_legacy", False)
        )
        llm_usage = None
        if can_view_global_llm_usage:
            # LLM token 用量统计（按 stage 聚合 + 总计），查询失败时
            # usage_recorder 内部已 fail-open 返回全零结构，不会导致整个
            # /stats 接口 500
            llm_usage = await asyncio.to_thread(usage_recorder.get_stats, days)

        return TranscribeResponse(
            code=200,
            message="获取统计信息成功",
            data={
                "user_stats": user_stats,
                "llm_usage": llm_usage,
                "is_multi_user_mode": user_manager.is_multi_user_mode(),
                "total_users": user_manager.get_user_count(),
            },
        )
    except Exception as exc:
        logger.exception("获取审计统计异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {exc}")


@router.get("/calls")
async def get_audit_calls(
    limit: int = Query(100, ge=1, le=10000, description="返回记录数量限制"),
    user_info: dict = Depends(verify_token),
):
    try:
        user_id = user_info.get("user_id")
        # audit_logger.get_recent_calls() 把 user_id 缺失设计成"查询全部
        # 用户的调用记录"（供未来管理员/CLI 场景使用），但这个 HTTP 端点是
        # 面向最终用户"查看自己的调用记录"的，绝不能因为调用方配置不完整
        # （多用户配置缺 user_id 字段，validate_token() 仍会签发该 token，
        # 见 user_manager.py::_load_users_config）就意外触发这个"全量"语义，
        # 把所有租户的 URL/IP/User-Agent/task_id 泄露给它——同一类 user_id
        # 缺失越权，在本轮 /summary 修复后本地 codex review 追加发现。
        if not user_id:
            logger.error("audit calls: caller has no user_id, denying access (fail-closed)")
            raise HTTPException(status_code=401, detail="无法确定调用方身份")
        recent_calls = await asyncio.to_thread(audit_logger.get_recent_calls, user_id, limit)
        return TranscribeResponse(
            code=200,
            message="获取调用记录成功",
            data={"calls": recent_calls, "user_id": user_id, "limit": limit},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("获取审计调用记录异常: %s", exc)
        raise HTTPException(status_code=500, detail=f"获取调用记录失败: {exc}")


@router.get("/history")
async def get_history(
    start_date: Optional[str] = Query(None, description="开始日期，ISO格式 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期，ISO格式 YYYY-MM-DD"),
    webhook: Optional[str] = Query(None, description="webhook地址精确过滤"),
    platform: Optional[str] = Query(None, description="平台过滤: youtube/bilibili/xiaoyuzhou/apple_podcast/xiaohongshu/douyin"),
    author: Optional[str] = Query(None, description="频道/作者过滤，支持逗号分隔多选"),
    q: Optional[str] = Query(None, description="关键词搜索：匹配标题、频道名、视频URL"),
    status: Optional[str] = Query(None, description="任务状态过滤，默认只显示 success（已完成）"),
    limit: int = Query(20, ge=1, le=10000, description="每页条数，客户端已读过滤时传大值"),
    offset: int = Query(0, ge=0, description="分页偏移"),
    user_info: dict = Depends(verify_token),
):
    """
    查询 audit.db 自有任务快照；不连接 cache.db。

    默认只返回已完成（completed）的任务，支持按日期、webhook、平台、频道过滤。
    """
    api_key = user_info.get("api_key", "")
    api_key_masked = audit_logger._mask_api_key(api_key)
    user_id = user_info.get("user_id")

    # 租户边界用 user_id 精确匹配，而非截断后的 api_key_masked（只保留前
    # 4/后 4 位，不同 key 长度相同时可能碰撞，会让 A 看到 B 的历史记录——
    # api_key_masked 保留在下面的 SELECT/响应体里纯粹是展示字段，不再充当
    # 鉴权边界，见 api_audit_logs 表已有的 user_id 列+索引（云端 CI codex
    # gate 发现）。
    #
    # 但仅靠 a.user_id 还不够：GET /api/task/{task_id} 是设计允许的"进度查询"
    # capability（任何认证用户都能轮询任意 task_id，见 routes/tasks.py::
    # get_task_status），而每次轮询同样会写一条 api_audit_logs 行——若历史
    # 列表只认"存在一条属于我的审计行"，观察者轮询一次别人的任务就足以让
    # 该任务（连同它在 task_audit_snapshots 里的真实标题/平台/状态）出现在
    # 自己的历史列表中，超出了"进度查询"本应有的边界（本地 codex review
    # 追加发现）。
    #
    # 历史归属改锚定在 task_audit_snapshots.submitted_by（任务创建时写入，
    # 只有 /api/transcribe、/api/recalibrate 会设置它——见 routes/tasks.py），
    # 而不是"任意一条审计行"。submitted_by 是本 PR 新增列，历史存量任务在
    # 升级那一刻该列必为 NULL；为不吞掉这些旧任务，NULL 时退回旧的审计行判断，
    # 但把范围收紧到 SUBMISSION_ENDPOINTS（提交类端点），排除进度查询端点，
    # 堵住同一个越权口子对旧数据同样成立。
    #
    # 驱动表反转（本地 codex review 第 6 轮 G1）：AuditLogger.log_api_call()
    # 会吞掉所有 SQLite 异常并返回 False，调用方从不检查这个返回值——一旦
    # 提交类端点那一行审计日志写失败，任务依然有完整、正确归属的
    # task_audit_snapshots 快照（submitted_by 由 create_task() 直接写入，
    # 不依赖审计日志是否写成功），但此前这里仍以 api_audit_logs a 为 FROM
    # 表，a 侧压根没有这一行时任务就永久从历史里消失了。
    #
    # 修复用 UNION ALL 合并两条独立查询（而不是单纯把驱动表换成
    # task_audit_snapshots）：
    #   分支一（权威路径，本轮新增）：FROM task_audit_snapshots s，按
    #     submitted_by 归属过滤，LEFT JOIN 提交类审计行只用于补充展示
    #     字段（video_url/wechat_webhook/api_key_masked/更精确的
    #     request_time）——即使这一行缺失，s 本身仍然入选。
    #   分支二（兼容路径，此前唯一的路径）：FROM api_audit_logs a，仅在
    #     NOT EXISTS 对应快照时才纳入。这条分支必须保留：快照要到任务
    #     first 归档（成功/失败）时才会出现，见 CacheManager.
    #     update_task_status 里对 archive_task_snapshot 的调用点；一个
    #     已提交但从未归档过快照的任务（例如仍在排队，或
    #     archive_task_snapshot 反复失败）此前也会在 status=all 时出现
    #     （test_left_join_null_task_included_when_status_all 锁死的既有
    #     行为），分支一覆盖不到这类"压根没有快照"的任务。NOT EXISTS 防止
    #     与分支一重复计数同一个 task_id：快照一旦存在，任务的去留完全由
    #     分支一的归属判定决定，不能被分支二用"存在审计行"重新捞回来
    #     （那样会让不属于自己、且未通过归属判定的任务绕过判定重新出现）。
    join_sql, join_params = _submission_log_join_condition()
    attribution_sql, attribution_params = _task_attribution_condition(user_id)
    endpoint_placeholders = ",".join("?" for _ in SUBMISSION_ENDPOINTS)

    combined_cte = f"""
        WITH combined AS (
            SELECT
                s.task_id AS task_id,
                a.video_url AS video_url,
                a.wechat_webhook AS wechat_webhook,
                COALESCE(a.request_time, s.completed_at, s.archived_at) AS request_time,
                a.api_key_masked AS api_key_masked,
                s.view_token AS view_token,
                s.title AS title,
                s.author AS author,
                s.platform AS platform,
                s.status AS status,
                s.calibration_status AS calibration_status,
                s.summary_status AS summary_status,
                s.chapters_status AS chapters_status,
                s.content_expired AS content_expired
            FROM task_audit_snapshots s
            LEFT JOIN api_audit_logs a ON a.task_id = s.task_id AND {join_sql}
            WHERE {attribution_sql}

            UNION ALL

            SELECT
                a.task_id AS task_id,
                a.video_url AS video_url,
                a.wechat_webhook AS wechat_webhook,
                a.request_time AS request_time,
                a.api_key_masked AS api_key_masked,
                NULL AS view_token,
                NULL AS title,
                NULL AS author,
                NULL AS platform,
                'unknown' AS status,
                NULL AS calibration_status,
                NULL AS summary_status,
                NULL AS chapters_status,
                0 AS content_expired
            FROM api_audit_logs a
            WHERE a.user_id = ?
              AND a.endpoint IN ({endpoint_placeholders})
              AND a.task_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM task_audit_snapshots s2 WHERE s2.task_id = a.task_id
              )
        )
    """
    cte_params = [*join_params, *attribution_params, user_id, *SUBMISSION_ENDPOINTS]

    conditions: list = []
    params: list = []

    if webhook:
        conditions.append("wechat_webhook = ?")
        params.append(webhook)

    if start_date:
        conditions.append("request_time >= ?")
        params.append(f"{start_date} 00:00:00")

    if end_date:
        conditions.append("request_time <= ?")
        params.append(f"{end_date} 23:59:59")

    if platform:
        conditions.append("platform = ?")
        params.append(platform)

    if author:
        author_list = [a.strip() for a in author.split(",") if a.strip()]
        if len(author_list) == 1:
            conditions.append("author = ?")
            params.append(author_list[0])
        elif author_list:
            placeholders = ",".join("?" * len(author_list))
            conditions.append(f"author IN ({placeholders})")
            params.extend(author_list)

    # status 默认 'success'；传 'all' 则不过滤。combined.status 恒非空
    # （分支一取自 NOT NULL 的 s.status，分支二固定字面量 'unknown'）。
    effective_status = _normalize_history_status(status)
    if effective_status != "all":
        conditions.append("status = ?")
        params.append(effective_status)

    # 关键词搜索：LIKE 匹配标题、频道名、视频URL
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(
            "(COALESCE(title, '') LIKE ? OR COALESCE(author, '') LIKE ? OR COALESCE(video_url, '') LIKE ?)"
        )
        params.extend([pattern, pattern, pattern])

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    base_sql = combined_cte + f"""
        SELECT
            task_id, video_url, wechat_webhook, request_time, api_key_masked,
            view_token, title, author, platform, status,
            calibration_status, summary_status, chapters_status, content_expired
        FROM combined
        WHERE {where_clause}
        ORDER BY request_time DESC
    """

    count_sql = combined_cte + f"""
        SELECT COUNT(*) FROM combined WHERE {where_clause}
    """

    exec_params = cte_params + params

    def _run_query():
        # Read only audit.db; snapshots are audit-owned.
        conn = sqlite3.connect(audit_logger.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("PRAGMA query_only = 1")

            cur = conn.cursor()
            cur.execute(count_sql, exec_params)
            total = cur.fetchone()[0]

            cur.execute(base_sql + " LIMIT ? OFFSET ?", exec_params + [limit, offset])
            rows = cur.fetchall()

            items = []
            for row in rows:
                items.append({
                    "task_id": row[0],
                    "video_url": row[1],
                    "wechat_webhook": row[2],
                    "request_time": row[3],
                    "api_key_masked": row[4],
                    "view_token": row[5],
                    "title": row[6],
                    "author": row[7],
                    "platform": row[8],
                    "status": row[9] or "unknown",
                    # 诚实状态模型字段：前端本次不强制消费，供后续 UI 迭代使用
                    "calibration_status": row[10],
                    "summary_status": row[11],
                    "chapters_status": row[12],
                    "content_expired": bool(row[13]),
                })
            return total, items
        finally:
            conn.close()

    # 锁竞争重试一次（50ms 间隔）；查询在线程池执行，避免阻塞事件循环
    try:
        total, items = await asyncio.to_thread(_run_query)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            logger.warning("history query: database locked, retrying in 50ms")
            await asyncio.sleep(0.05)
            try:
                total, items = await asyncio.to_thread(_run_query)
            except sqlite3.OperationalError as e2:
                logger.error("history query: database still locked after retry: %s", e2)
                raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
        else:
            logger.exception("history query failed: %s", e)
            raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    except Exception as exc:
        logger.exception("history query unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")

    return TranscribeResponse(
        code=200,
        message="获取历史记录成功",
        data={
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "api_key_masked": api_key_masked,
        },
    )


@router.get("/filter-options")
async def get_filter_options(user_info: dict = Depends(verify_token)):
    """
    获取当前 API Key 下历史出现的所有 webhook、平台、频道名称，用于前端过滤下拉框。
    各选项按出现频次倒序，最多返回 50 条。
    """
    user_id = user_info.get("user_id")
    join_sql, join_params = _submission_log_join_condition()
    attribution_sql, attribution_params = _task_attribution_condition(user_id)

    def _run_query():
        conn = sqlite3.connect(audit_logger.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("PRAGMA query_only = 1")
            cur = conn.cursor()

            # 租户边界用 user_id，不用可能碰撞的 api_key_masked（云端 CI codex gate）
            # 历史 webhook 列表（按频次倒序）。webhook 直接来自本人发起调用时
            # 自己传入的参数（不经 task_audit_snapshots JOIN），天然不存在
            # "看到别人任务的 webhook" 的越权路径，因此不需要 _task_attribution_
            # condition：a.user_id = ? 已经是完整边界。
            cur.execute("""
                SELECT wechat_webhook, COUNT(*) as cnt
                FROM api_audit_logs
                WHERE user_id = ? AND wechat_webhook IS NOT NULL
                GROUP BY wechat_webhook
                ORDER BY cnt DESC
                LIMIT 50
            """, (user_id,))
            webhooks = [row[0] for row in cur.fetchall()]

            # 历史平台列表。驱动表反转（本地 codex review 第 6 轮 G1，见
            # get_history()/_task_attribution_condition 的详细动机）：此前
            # 以 api_audit_logs a 为 FROM 表，提交类端点的审计行缺失时，该
            # 任务的平台会连同它一起从下拉框里消失。现在以
            # task_audit_snapshots s 为驱动表，a 只用于 legacy（submitted_by
            # 为 NULL）归属回退，不影响 s 本身的去留。platform 这个展示字段
            # 完全来自 s，不受 a 缺失影响。
            cur.execute(f"""
                SELECT s.platform, COUNT(*) as cnt
                FROM task_audit_snapshots s
                LEFT JOIN api_audit_logs a ON a.task_id = s.task_id AND {join_sql}
                WHERE {attribution_sql} AND s.platform IS NOT NULL
                GROUP BY s.platform
                ORDER BY cnt DESC
            """, (*join_params, *attribution_params))
            platforms = [row[0] for row in cur.fetchall()]

            # 历史频道/作者列表（按频次倒序）。同上，驱动表反转为
            # task_audit_snapshots。
            cur.execute(f"""
                SELECT s.author, COUNT(*) as cnt
                FROM task_audit_snapshots s
                LEFT JOIN api_audit_logs a ON a.task_id = s.task_id AND {join_sql}
                WHERE {attribution_sql} AND s.author IS NOT NULL AND s.author != ''
                GROUP BY s.author
                ORDER BY cnt DESC
                LIMIT 50
            """, (*join_params, *attribution_params))
            authors = [row[0] for row in cur.fetchall()]

            return webhooks, platforms, authors
        finally:
            conn.close()

    try:
        webhooks, platforms, authors = await asyncio.to_thread(_run_query)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            await asyncio.sleep(0.05)
            try:
                webhooks, platforms, authors = await asyncio.to_thread(_run_query)
            except sqlite3.OperationalError as e2:
                logger.error("filter-options: database still locked: %s", e2)
                raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
        else:
            logger.exception("filter-options query failed: %s", e)
            raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    except Exception as exc:
        logger.exception("filter-options unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail=f"查询失败: {exc}")

    return TranscribeResponse(
        code=200,
        message="获取过滤选项成功",
        data={
            "webhooks": webhooks,
            "platforms": platforms,
            "authors": authors,
        },
    )


def check_view_token_ownership(
    view_token: str,
    task_id: str,
    user_id: Optional[str],
    cache_manager,
    audit_logger,
) -> bool:
    """view_token 归属判定（本地 codex review 第 4/5/6/7/8 轮持续加固）。

    本地 codex review 第 8 轮 K1：从 get_task_summary 内部的 _check_ownership
    闭包抽成模块级函数，供 routes/tasks.py::recalibrate 复用同一套判定
    逻辑——此前 recalibrate 只检查通用 recalibrate 权限 + view_token 存在，
    不核验原任务归属，任何有 recalibrate 权限的用户拿到别人公开分享的
    view_token 就能触发重新处理，覆盖共享媒体的校对/说话人产物、消耗对方
    的 LLM 配额。GET /view/{token} 本身的公开只读语义不受影响：只有会产生
    副作用（写库 + 消耗 LLM 配额）的写操作才需要核验调用方是该 view_token
    关联任务的权威提交者。

    create_task() 明确允许同一 URL 的重复提交共享同一个 view_token（见
    CacheManager.create_task），因此一个 view_token 背后可能挂着多个
    task_id，分属不同提交者。get_task_by_view_token() 只按优先级（success
    优先、同级按 created_at 取最新）选出*一条*用于内容展示——这是内容
    选择，不是归属判定，因此下面枚举的是"该 view_token 关联的全部
    task_id"，不只是被选中展示的那一个。

    证据优先（取代此前"分层查找、逐级默认放行"的结构）：先把该
    view_token 下所有已知 task_id 的 submitted_by 证据一次性收集齐（覆盖
    task_audit_snapshots 与 cache.db 的 task_status 两个数据源，任一处
    非空即采信——但已明确撤销的 task_id 无论出现在哪个数据源，都不写入
    这份证据映射，见下方 L3 说明），再按证据强度统一判定：
      1. 任一 task_id 的 submitted_by 命中当前用户 -> 放行（正面归属
         证据，视图内容选择与授权解耦）。
      2. 否则，对 submitted_by 仍未知、且该 task_id 从未被明确撤销
         （task_audit_snapshots 里既没有 content_expired=1 的快照、
         cache.db 也查不到 submitted_by——纯 legacy 存量任务）的 task_id
         逐个走 _legacy_owns_task 的审计行兜底；命中即放行。已明确撤销
         （content_expired=1）的 task_id 直接跳过、不进入 legacy 兜底
         （Z1，PR3 review hardening 本轮）——撤销是"归属结论已知且为否"，
         不能退回"归属未知"分支被历史提交行重新洗白。
      3. 上述两步都没有找到放行证据——无论是因为存在非空 submitted_by
         但都不是当前用户（正面排他证据），还是纯 legacy 场景下审计行
         兜底也查无记录（完全无归属信息可考），或候选本身已被明确撤销——
         一律 fail-closed 拒绝，不再有"审计缺失就默认放行"的出口。

    L3（CI review 第 5 轮 P1）：已明确撤销的 task_id 不能经任何路径提供
    正面授权——此前 cache 候选路径（list_tasks_by_view_token 的结果）完全
    不检查 revoked_task_ids，只有 legacy 兜底路径检查（Z1）；revoked_
    task_ids 却比第 1 步的 `any(v == user_id ...)` 检查晚构建完成检查时机
    ——cache 候选若恰好也命中一个已撤销的 task_id，其原始 submitted_by 会
    在第 1 步被直接采信，撤销被绕过。现在 cache 候选循环与 legacy 兜底
    循环使用同一份 revoked_task_ids 集合、同一条排除规则。

    Args:
        view_token: 目标 view_token。
        task_id: 已知的一个关联 task_id（作为归属证据收集的起点之一，
            即便它本身既不在 task_audit_snapshots 也不在 cache.db 的
            task_status 里出现，也会被纳入候选集合参与 legacy 兜底判定，
            与此前 _owns_task 总是直接查一次 primary task_id 的行为
            保持一致）。
        user_id: 当前调用方的 user_id。为 None 时不参与"正面归属证据"
            比较（Python `None == None` 会让缺失 user_id 的调用方被任何
            submitted_by 同样为 NULL 的历史遗留任务误判为匹配，等同于
            绕过归属校验）。
        cache_manager: 提供 list_tasks_by_view_token 的 CacheManager 实例。
        audit_logger: 提供 _get_cursor 的 AuditLogger 实例。

    Returns:
        bool: True 表示调用方对该 view_token 下至少一个任务拥有权威归属
        （或 legacy 审计行兜底通过），可以放行；否则 fail-closed 拒绝。
    """

    def _legacy_owns_task(candidate_task_id: str) -> bool:
        """纯 legacy 兜底：某个 task_id 在所有已知来源里都查不到
        submitted_by（既不在 task_audit_snapshots，也不在 cache.db 的
        task_status——本 PR 迁移前的存量任务，或列本身从未写入过）时，
        退回旧的审计行判断。只认提交类端点（/api/transcribe、
        /api/recalibrate）产生的那一行，绝不能让 GET /api/task/{task_id}
        的轮询行当归属证据——那是设计允许的进度查询 capability，不能被
        这里的归属校验放大成摘要内容访问权限/重新处理触发权限。
        """
        placeholders = ",".join("?" for _ in SUBMISSION_ENDPOINTS)
        with audit_logger._get_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM api_audit_logs "
                f"WHERE task_id = ? AND user_id = ? AND endpoint IN ({placeholders}) "
                "LIMIT 1",
                (candidate_task_id, user_id, *SUBMISSION_ENDPOINTS),
            )
            return cursor.fetchone() is not None

    # 收集该 view_token 下所有已知 task_id 的 submitted_by。显式预置
    # primary task_id -> None：即便它既不在下面两个查询的结果集里出现
    # （例如测试里精确控制 mock 返回值的场景），也必须留在候选集合里参与
    # 后续的 legacy 兜底判定。
    submitted_by_by_task: dict = {task_id: None}

    # Z1（PR3 review hardening 本轮，fail-closed 修复）：单独记录"明确已
    # 撤销"（content_expired=1）的 task_id 集合，与"纯 legacy（快照压根
    # 不存在，或存在但 submitted_by 为 NULL 且未撤销）"区分开。此前这里
    # 的 WHERE 直接加 COALESCE(content_expired, 0) = 0，把已撤销快照的行
    # 整个过滤掉、不写回 submitted_by_by_task——但 621 行预置的
    # {task_id: None} 和下面兜底循环都只按"submitted_by is None"判断是否
    # 该走 legacy，分不清"从未归档过的纯 legacy 任务"和"归档过又被明确
    # 撤销的任务"，后者会被误当作前者，经 _legacy_owns_task 查到的历史
    # 提交审计行重新拿到摘要/recalibrate 授权——撤销的单调性被绕过。
    # 现在改为不按 content_expired 过滤、取出全部行：content_expired=1 的
    # 行只记进 revoked_task_ids，不写回 submitted_by_by_task（撤销快照的
    # submitted_by 依旧不能当正面归属证据，这一点语义与此前保持一致）；
    # 未撤销的行照旧走原有的更新逻辑。
    revoked_task_ids: set = set()
    with audit_logger._get_cursor() as cursor:
        cursor.execute(
            "SELECT task_id, submitted_by, content_expired FROM task_audit_snapshots "
            "WHERE (view_token = ? OR task_id = ?)",
            (view_token, task_id),
        )
        for row_task_id, row_submitted_by, row_content_expired in cursor.fetchall():
            if row_content_expired:
                revoked_task_ids.add(row_task_id)
                continue
            if row_submitted_by is not None or row_task_id not in submitted_by_by_task:
                submitted_by_by_task[row_task_id] = row_submitted_by

    for candidate in cache_manager.list_tasks_by_view_token(view_token):
        candidate_task_id = candidate.get("task_id")
        if not candidate_task_id:
            continue
        # L3 修复（CI review 第 5 轮 P1）：已明确撤销的 task_id 不能经 cache
        # 候选路径重新提供正面归属证据——此前这里完全不检查 revoked_task_ids
        # （那个集合只在下面 legacy 兜底循环里被检查），撤销 task 若恰好也
        # 出现在 list_tasks_by_view_token 的结果里（cache_manager 是否已经
        # 自行按 content_expired 过滤，不能假定——真实 CacheManager 有这层
        # 过滤是 R1 修复的结果，但这里传入的可能是任何实现），它的原始
        # submitted_by 会被写进 submitted_by_by_task，紧接着下面 662 行的
        # `any(v == user_id ...)` 检查跑在 revoked_task_ids 过滤之前，原提交
        # 者就能绕开撤销重新拿到正面授权。与下面 671 行 legacy 兜底循环的
        # 撤销防护（Z1）同一原则：撤销任务不能经任何路径提供正面授权。
        if candidate_task_id in revoked_task_ids:
            continue
        candidate_submitted_by = candidate.get("submitted_by")
        if candidate_submitted_by is not None or candidate_task_id not in submitted_by_by_task:
            submitted_by_by_task[candidate_task_id] = candidate_submitted_by

    if user_id is not None and any(
        v == user_id for v in submitted_by_by_task.values()
    ):
        return True

    for candidate_task_id, submitted_by in submitted_by_by_task.items():
        # 已明确撤销的 task_id 不再走 legacy 兜底：撤销是"归属结论已知且为
        # 否"，不能退回"归属未知，靠历史提交行推定"的分支——否则任何撤销
        # 都能被旧提交审计行重新洗白，破坏撤销的单调性（Z1）。
        if candidate_task_id in revoked_task_ids:
            continue
        if submitted_by is None and _legacy_owns_task(candidate_task_id):
            return True

    return False


@router.get("/summary")
async def get_task_summary(
    view_token: str = Query(..., description="任务的 view_token"),
    user_info: dict = Depends(verify_token),
):
    """
    获取任务摘要预览（前 300 字），用于历史页面 hover 展示。
    校验 view_token 归属当前 API Key，防止跨用户读取。
    """
    user_id = user_info.get("user_id")
    cache_manager = get_cache_manager()

    # 通过 view_token 查任务信息（同步 SQLite 调用，线程池执行）
    task_info = await asyncio.to_thread(cache_manager.get_task_by_view_token, view_token)
    if not task_info:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 校验归属：task_info 中暂无 user_id，通过 audit_log 反查（租户边界用
    # user_id，不用可能碰撞的 api_key_masked，见云端 CI codex gate）。
    # 若查不到任何关联审计记录，允许访问（兼容早期未落审计日志的旧任务）；
    # 但查询本身抛异常（数据库锁/连接问题等）时，绝不能等同于"允许访问"——
    # 之前的 fail-open 会让任何认证用户在审计库异常时读到不属于自己的摘要，
    # 这里改为 fail-closed，异常时拒绝访问并返回 503（云端 CI codex gate）。
    #
    # 注意：条件只看 task_id，不再要求 user_id 非空（本地 codex review 追加
    # 发现）——多用户配置里缺失 user_id 字段时，_load_users_config() 只记
    # warning、并不拒绝该配置项，validate_token() 依然会正常签发这个
    # user_info（user_id 为 None）。若这里保留 "and user_id"，配置不完整
    # 的租户会被直接跳过整段归属校验，等同于对任何 view_token 都放行。
    # user_id=None 传入下面的参数化查询会被 SQLite 翻译成 SQL NULL，
    # "user_id = NULL" 语义上不匹配任何行（即使目标行的 user_id 也是
    # NULL），因此天然会落到"有记录但不属于当前调用方"的拒绝分支，行为
    # 仍是安全的 fail-closed，不需要特殊分支。
    task_id = task_info.get("task_id")
    if task_id:
        # 归属判定逻辑（本地 codex review 第 8 轮 K1 抽成模块级
        # check_view_token_ownership，供 routes/tasks.py::recalibrate 复用
        # 同一套判定——见该函数的完整 docstring）。
        try:
            owned = await asyncio.to_thread(
                check_view_token_ownership,
                view_token, task_id, user_id, cache_manager, audit_logger,
            )
        except Exception as e:
            logger.error("summary auth check failed, denying access (fail-closed): %s", e)
            raise HTTPException(status_code=503, detail="归属校验暂时不可用，请稍后重试")
        if not owned:
            raise HTTPException(status_code=403, detail="无权访问该任务")

    # 任务未完成（cache.db 中完成的状态值为 'success'）
    task_status = task_info.get("status", "")
    if task_status not in ("success",):
        return TranscribeResponse(
            code=202,
            message="任务处理中",
            data={"summary": "", "status": task_status},
        )

    # 获取摘要：复用 ViewTokenResolver 的视图数据逻辑
    try:
        view_data = await asyncio.to_thread(
            ViewTokenResolver(cache_manager).get_view_data_by_token, view_token
        )
        if not view_data or view_data.get("status") not in ("success",):
            return TranscribeResponse(
                code=200,
                message="摘要不可用",
                data={"summary": None, "status": view_data.get("status") if view_data else "unknown"},
            )

        # 诚实状态模型：不再把"总结处理中..."之类的占位字符串当真实摘要返回。
        # summary_state 由 ViewTokenResolver.get_view_data_by_token 提供
        # （generated/skipped_short/failed/pending）；只有 generated 才有真实文本。
        raw_summary = view_data.get("summary")
        summary_state = view_data.get("summary_state")
        if summary_state is None:
            # 向后兼容：view_data 尚未带 summary_state 字段（如旧版 cache_manager
            # 或手工构造的 mock），退回旧的"非空字符串即视为已生成"启发式判断。
            summary_state = SummaryStatus.GENERATED if raw_summary else None

        if summary_state == SummaryStatus.GENERATED and raw_summary:
            # 取前 300 个 Unicode 字符
            preview = raw_summary[:300]
            return TranscribeResponse(
                code=200,
                message="获取摘要成功",
                data={"summary": preview, "status": "success", "summary_status": summary_state},
            )

        return TranscribeResponse(
            code=200,
            message="摘要不可用",
            data={"summary": None, "status": "success", "summary_status": summary_state},
        )
    except Exception as exc:
        logger.exception("get summary failed for view_token=%s: %s", view_token, exc)
        raise HTTPException(status_code=500, detail=f"获取摘要失败: {exc}")
