<!-- delegate-outcome: succeeded -->
# PR201 三项运行探针

固定 head：`3a25dccc2cb92e22b4031715c37a584c041e582d`。只改本证据文件；所有 SQLite、缓存目录和 audit.db 均在 `TemporaryDirectory`，未读写 repo 配置。先以 mkdtemp 写入确认权限，再 `uv sync --frozen`（仅本 worktree `.venv`），`PYTHONPATH=src` 导入真实 `CacheManager`。无 worker/HTTP 端点；不外推到生产。

## 判定

1. **preterminal cache deletion：默认完整清理路径已证伪。** register/accept + `save_cache` 真写入正文；`local_upload_share_is_active=True`，而 `_has_active_local_upload_for_media=False`（`expires_at=NULL` 的 SQL 条件缺失）。但 `cleanup_old_cache` 先命中 queued/processing/calibrating task guard，三份文件与行均保留。撤销且 failed 上传、URL 对照由完整入口删除（2 行），不是 0 候选。仅用临时 SQL 把 `updated_at` 设为 31 天前以越过年龄筛选，不代表自然发生。覆盖 `update_task_status` 文档列出的全部非终态。后续：lead 决定是否统一两个 predicate 的语义；本次未证明实际清理丢失。
2. **task cleanup FK crash：默认路径已证伪；FK ON 条件反例复现。** 默认 `_get_connection()` 实测 `foreign_keys=0`，真实 `cleanup_task_status` 无异常并删 root；receipt 保留、root_task_id 成孤儿（1）。同流程显式 `PRAGMA foreign_keys=ON` 后实际清理抛 `IntegrityError`，事务回滚，root/receipt 保留。`rg -n "PRAGMA foreign_keys" src` 无命中；CacheManager、AuditLogger 与只读 SQL 构造均未开启它。条件反例不代表默认路径或 P1。后续：lead 决定是否约束外部启用 FK 的契约，以及默认路径孤儿 receipt 是否符合历史查询需要。
3. **accept after 24h：默认路径可复现。** 窗内 register，跨窗 24h+1s 调真实 accept 得 `ValueError`；DB 仍是 `receiving`、无 root。过窗重复 register 仍读出原 receipt。契约写明 server 验证 24h acceptance window、正式接受后才返回 202（design.md:35,42）；按此字面拒绝符合约定。design 同时写“首次登记”窗口，若意为只校验 register，则与 accept 二次校验有歧义，需 lead 定义时点；本证据不改逻辑。

## 运行输出

探针1（旧年龄为受控条件；producer 文件及完整 consumer 路径）：

```text
queued: share=True sql_guard=False root=queued expiry=none file_before=True file_after=True
processing: share=True sql_guard=False root=processing expiry=none file_before=True file_after=True
calibrating: share=True sql_guard=False root=calibrating expiry=none file_before=True file_after=True
inactive: share=False sql_guard=False root=failed expiry=set file_before=True file_after=False
url: file_before=True file_after=False
cleanup_deleted=2 cache_rows=5->3
```

探针2（临时 AuditLogger/DB；终态 root；now 控制为完成后 61 天以满足过期条件）：

```text
foreign_keys=0 outcome=ok deleted=1 root=1->0 upload=1->1 orphan=1
foreign_keys=1 outcome=IntegrityError deleted=na root=1->1 upload=1->1 orphan=0
```

探针3：

```text
accept_exception=ValueError receipt_state=receiving root_task_id=null root_rows=0
```

## 可复制探针

从仓库根目录运行；仅输出状态、计数、异常类型与文件存在性，不打印路径。

### 1. cache 清理

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from datetime import datetime, timedelta, timezone; from pathlib import Path; import tempfile, uuid
from loguru import logger; from video_transcript_api.cache.cache_manager import CacheManager
logger.remove(); now = datetime.now(timezone.utc)
old = (now - timedelta(days=31)).strftime('%Y-%m-%d %H:%M:%S')
with tempfile.TemporaryDirectory() as td:
    base = Path(td); m = CacheManager(str(base/'cache'), str(base/'cache/cache.db')); cases = {}
    def make_upload(label, status=None, revoked=False):
        mid = 'probe-' + label; key = f"{int(now.timestamp()*1000)}-{uuid.uuid4()}"
        u = m.register_local_upload(owner_user_id='owner', idempotency_key=key, retention='30d', now=now)
        a = m.accept_local_upload(u['upload_id'], media_id=mid, task_id='task-'+label, now=now)
        if status in ('processing','calibrating'): m.update_task_status(a['root_task_id'], status, suppress_terminal_notification=True)
        saved = m.save_cache('local_upload', '', mid, False, 'producer-body', 'capswriter')
        if not saved: raise RuntimeError('save_cache failed')
        if status == 'failed': m.update_task_status(a['root_task_id'], status, suppress_terminal_notification=True)
        if revoked: m.revoke_local_upload(u['upload_id'], now=now)
        with m._get_cursor() as c: c.execute('UPDATE video_cache SET updated_at=? WHERE media_id=?', (old, mid))
        with m._get_cursor() as c:
            c.execute('SELECT u.*, t.status root_status FROM local_uploads u JOIN task_status t ON t.task_id=u.root_task_id WHERE u.media_id=?', (mid,))
            row = dict(c.fetchone()); c.execute('SELECT 1 FROM video_cache WHERE media_id=?', (mid,)); before = c.fetchone() is not None
        with m._get_cursor() as c: sql_active = m._has_active_local_upload_for_media(c, 'local_upload', mid, m._utc_sql_timestamp(now))
        cases[label] = (row, CacheManager.local_upload_share_is_active(row, now), sql_active, base/'cache'/saved['files_loc'], before)
    for s in ('queued','processing','calibrating'): make_upload(s, s)
    make_upload('inactive', 'failed', True)
    saved = m.save_cache('youtube', 'https://example.invalid/v', 'url-control', False, 'url-body', 'capswriter')
    if not saved: raise RuntimeError('URL save_cache failed')
    with m._get_cursor() as c: c.execute('UPDATE video_cache SET updated_at=? WHERE media_id=?', (old, 'url-control'))
    url_path = base/'cache'/saved['files_loc']
    with m._get_cursor() as c: before_rows = c.execute('SELECT COUNT(*) FROM video_cache').fetchone()[0]
    deleted = m.cleanup_old_cache(days=30, now=now)
    with m._get_cursor() as c: after_rows = c.execute('SELECT COUNT(*) FROM video_cache').fetchone()[0]
    for label, (row, active, sql_active, path, before) in cases.items():
        print(f"{label}: share={active} sql_guard={sql_active} root={row['root_status']} expiry={'none' if row['expires_at'] is None else 'set'} file_before={before} file_after={path.exists()}")
    print(f"url: file_before=True file_after={url_path.exists()}"); print(f"cleanup_deleted={deleted} cache_rows={before_rows}->{after_rows}")
    m._get_connection().close()
PY
```

### 2. task cleanup / FK

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from datetime import datetime, timedelta, timezone; from pathlib import Path; import tempfile, uuid
from loguru import logger; from video_transcript_api.cache.cache_manager import CacheManager
from video_transcript_api.utils.logging.audit_logger import AuditLogger
logger.remove()
def run(force_fk):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td); m = CacheManager(str(base/'cache'), str(base/'cache/cache.db'))
        audit = AuditLogger(str(base/'audit.db')); m.audit_logger = audit; conn = m._get_connection()
        if force_fk: conn.execute('PRAGMA foreign_keys=ON')
        fk = conn.execute('PRAGMA foreign_keys').fetchone()[0]
        now = datetime.now(timezone.utc); late = now + timedelta(days=61); key = f"{int(now.timestamp()*1000)}-{uuid.uuid4()}"
        u = m.register_local_upload(owner_user_id='owner', idempotency_key=key, retention='30d', now=now)
        a = m.accept_local_upload(u['upload_id'], media_id='fk-probe', task_id='task-fk', now=now)
        m.update_task_status(a['root_task_id'], 'failed', suppress_terminal_notification=True)
        with m._get_cursor() as c:
            c.execute('SELECT COUNT(*) FROM task_status WHERE task_id=?', (a['root_task_id'],)); rb = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM local_uploads WHERE upload_id=?', (u['upload_id'],)); ub = c.fetchone()[0]
        try: deleted = str(m.cleanup_task_status(0, now=late)); outcome = 'ok'
        except Exception as e: deleted = 'na'; outcome = type(e).__name__
        with m._get_cursor() as c:
            c.execute('SELECT COUNT(*) FROM task_status WHERE task_id=?', (a['root_task_id'],)); ra = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM local_uploads WHERE upload_id=?', (u['upload_id'],)); ua = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM local_uploads u LEFT JOIN task_status t ON t.task_id=u.root_task_id WHERE u.upload_id=? AND t.task_id IS NULL', (u['upload_id'],)); orphan = c.fetchone()[0]
        print(f"foreign_keys={fk} outcome={outcome} deleted={deleted} root={rb}->{ra} upload={ub}->{ua} orphan={orphan}")
        conn.close(); audit.close()
run(False); run(True)
PY
```

### 3. 窗口过期后正式 accept

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from datetime import datetime, timedelta, timezone; from pathlib import Path; import tempfile, uuid
from loguru import logger; from video_transcript_api.cache.cache_manager import CacheManager
logger.remove()
with tempfile.TemporaryDirectory() as td:
    base = Path(td); m = CacheManager(str(base/'cache'), str(base/'cache/cache.db'))
    start = datetime.now(timezone.utc); late = start + timedelta(hours=24, seconds=1)
    key = f"{int(start.timestamp()*1000)}-{uuid.uuid4()}"
    u = m.register_local_upload(owner_user_id='owner', idempotency_key=key, retention='30d', now=start)
    try: m.accept_local_upload(u['upload_id'], media_id='late-probe', task_id='task-late', now=late); exc = 'none'
    except Exception as e: exc = type(e).__name__
    receipt = m.register_local_upload(owner_user_id='owner', idempotency_key=key, retention='30d', now=late)
    with m._get_cursor() as c: c.execute('SELECT COUNT(*) FROM task_status WHERE task_id=?', ('task-late',)); roots = c.fetchone()[0]
    print(f"accept_exception={exc} receipt_state={receipt['state']} root_task_id={'null' if receipt['root_task_id'] is None else 'set'} root_rows={roots}")
    m._get_connection().close()
PY
```

## 约束与验收

固定 H0：`docs/sessions/261008-local-upload/design.md`。相关锁定测试名：`test_cleanup_protects_active_upload_cache_even_when_feature_is_disabled`、`test_cleanup_reclaims_revoked_or_expired_upload_cache`、`test_task_cleanup_keeps_effective_share_root_for_audit_and_url_rows_still_expire`、`test_expired_or_revoked_upload_task_rows_follow_existing_cleanup`、`test_real_sqlite_upload_identity_owner_key_and_intent_window`。只执行运行探针及卡面指定文件存在性判据，未跑 pytest/CI。

主干基线派发时不可用（`gh api request failed`），继承红未能判定；本卡没有新 CI 红。未改应用/测试/workflow。

## pickup 巡检

open issue 总数：3
summary: orphan 0 owned 0 unattributable 0 too-new 0 recent-7d 0 stale-over-7d 0 missing_ledger_repos 0
memory 巡检报告不可用：memory_dir_mismatch；未复用其他会话报告。

接手锚点：无交接单。当前分支 `card/vta-upload-A-probes-261008`，派发简报中 dispatch 即本任务本身；未将其视作他人占用。
