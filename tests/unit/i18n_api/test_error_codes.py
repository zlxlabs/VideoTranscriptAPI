"""API error payload and localization contract tests."""

import ast
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_transcript_api.api.context import ApiError, install_api_error_handlers
from video_transcript_api.i18n import SUPPORTED_LANGUAGES, t
from video_transcript_api.utils.logging.logger import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCALES_DIR = PROJECT_ROOT / "src" / "video_transcript_api" / "i18n" / "locales"
_CJK = re.compile(r"[\u3400-\u9fff]")


@pytest.fixture
def language_config():
    config = load_config()
    had_key = "language" in config
    previous = config.get("language")

    def set_language(language):
        if language is None:
            config.pop("language", None)
        else:
            config["language"] = language

    try:
        yield set_language
    finally:
        if had_key:
            config["language"] = previous
        else:
            config.pop("language", None)


_STATIC_BASE_DETAILS = [
    ("上传分享不可用", "upload_share_unavailable"),
    ("上传分享不可用或已关闭", "upload_share_closed"),
    ("视频URL不能为空", "invalid_url"),
    ("任务处理已达上限，请稍后重试", "task_capacity_reached"),
    ("service busy or lock unavailable, retry shortly", "service_busy"),
    ("任务队列已满，请稍后重试", "transcription_queue_full"),
    ("LLM 队列已满，请稍后重试", "llm_queue_full"),
    ("无重新校对权限", "recalibration_permission_denied"),
    ("未找到对应的转录数据", "transcript_not_found"),
    ("缓存中没有转录数据，无法重新校对", "recalibration_transcript_missing"),
    ("归属校验暂时不可用，请稍后重试", "ownership_check_unavailable"),
    ("无权对该任务发起重新校对", "recalibration_ownership_denied"),
    ("无重新生成总结权限", "resummary_permission_denied"),
    ("缓存中没有转录数据，无法重新生成总结", "resummary_transcript_missing"),
    ("无权对该任务发起重新生成总结", "resummary_ownership_denied"),
    ("总结已存在，无需重新生成", "summary_already_exists"),
    ("无生成详细笔记权限", "notes_permission_denied"),
    ("无权对该任务生成详细笔记", "notes_ownership_denied"),
    ("缓存中没有转录数据，无法生成详细笔记", "notes_transcript_missing"),
    ("详细笔记依赖已生成的章节层，请先生成章节后再重试", "notes_chapters_required"),
    ("详细笔记正在生成中，请等待当前任务完成", "notes_already_in_progress"),
    ("详细笔记已存在，无需重复生成", "notes_already_exists"),
    ("详细笔记任务状态查询失败，请稍后重试", "notes_status_unavailable"),
    ("无编辑分享页元数据权限", "metadata_permission_denied"),
    ("无权编辑该分享页的标题或作者", "metadata_ownership_denied"),
    ("该媒体存在其他账号提交的任务，禁止修改", "metadata_shared_denied"),
    ("该媒体仍有进行中的任务，结束后才能修改标题或作者", "metadata_in_flight"),
    ("title/author 至少提供一项，且不能为空白", "metadata_invalid"),
    ("无权访问全局总结比例统计", "summary_ratio_permission_denied"),
    ("无法确定调用方身份", "caller_identity_missing"),
    ("source 仅支持 upload 或 url", "invalid_history_source"),
    ("status must be one of: success, failed, all", "invalid_history_status"),
    ("上传历史查询失败", "upload_history_unavailable"),
    ("服务暂时不可用，请稍后重试", "service_unavailable"),
    ("任务不存在或分享已关闭", "task_not_found_or_share_closed"),
    ("任务不存在", "task_not_found"),
    ("无权访问该任务", "task_access_denied"),
    ("上传回执不存在", "upload_receipt_not_found"),
    ("上传记录不存在", "upload_record_not_found"),
    ("尚未正式受理的上传没有分享可关闭", "upload_not_accepted"),
    ("未提供授权令牌", "authorization_token_missing"),
    ("授权令牌格式错误", "authorization_token_malformed"),
    ("授权令牌无效", "authorization_token_invalid"),
]

_NOTE_ERROR_CODES = {
    "llm_queue_full",
    "transcription_queue_full",
    "recalibration_formatting_failed",
    "resummary_formatting_failed",
    "task_queue_failed",
    "notes_queue_failed",
}


def _sample_error_values(error_code):
    if error_code == "task_id_not_found":
        values = {"task_id": "probe-task"}
    elif error_code in {code for _, code in _DYNAMIC_BASE_DETAILS}:
        values = {"error": "probe-error"}
    else:
        values = {}
    if error_code in _NOTE_ERROR_CODES:
        values["note"] = ""
    return values


_DYNAMIC_BASE_DETAILS = [
    ("任务不存在: probe-task", "task_id_not_found"),
    ("获取任务状态失败: probe-error", "task_status_failed"),
    ("提交转录任务失败: probe-error", "transcription_submission_failed"),
    ("创建重新校对任务失败: probe-error", "recalibration_creation_failed"),
    ("重新校对任务转录数据格式化失败: probe-error", "recalibration_formatting_failed"),
    ("重新生成总结任务转录数据格式化失败: probe-error", "resummary_formatting_failed"),
    ("创建重新生成总结任务失败: probe-error", "resummary_creation_failed"),
    ("创建详细笔记任务失败: probe-error", "notes_creation_failed"),
    ("详细笔记任务加入队列失败: probe-error", "notes_queue_failed"),
    ("任务加入队列失败: probe-error", "task_queue_failed"),
    ("获取统计信息失败: probe-error", "audit_stats_failed"),
    ("获取总结比例统计失败: probe-error", "summary_ratio_failed"),
    ("获取调用记录失败: probe-error", "audit_calls_failed"),
    ("上传历史查询失败: probe-error", "upload_history_failed"),
    ("查询失败: probe-error", "audit_query_failed"),
    ("获取摘要失败: probe-error", "summary_fetch_failed"),
    ("获取用户配置失败: probe-error", "user_profile_failed"),
]


def test_json_api_sources_do_not_call_http_exception():
    route_files = [
        path for path in (PROJECT_ROOT / "src" / "video_transcript_api" / "api" / "routes").glob("*.py")
        if path.name != "views.py"
    ]
    route_files.append(
        PROJECT_ROOT / "src" / "video_transcript_api" / "api" / "services" / "transcription.py"
    )
    offending_calls = []
    for path in route_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Name) and function.id == "HTTPException"
                or isinstance(function, ast.Attribute) and function.attr == "HTTPException"
            ):
                offending_calls.append(f"{path}:{node.lineno}")

    assert not offending_calls, f"Replace HTTPException calls with ApiError: {offending_calls}"


@pytest.mark.parametrize(("source_detail", "expected_code"), _STATIC_BASE_DETAILS)
def test_static_api_details_match_base_and_have_stable_codes(
    language_config, source_detail, expected_code
):
    language_config("zh-CN")

    error = ApiError(
        status_code=400,
        error_code=expected_code,
        **_sample_error_values(expected_code),
    )

    assert error.detail == source_detail
    assert error.error_code == expected_code


@pytest.mark.parametrize(("source_detail", "expected_code"), _DYNAMIC_BASE_DETAILS)
def test_dynamic_api_details_match_base_and_have_stable_codes(
    language_config, source_detail, expected_code
):
    language_config("zh-CN")

    error = ApiError(
        status_code=500,
        error_code=expected_code,
        **_sample_error_values(expected_code),
    )

    assert error.detail == source_detail
    assert error.error_code == expected_code


@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize(
    "error_code", ["transcription_queue_full", "llm_queue_full"]
)
def test_queue_cleanup_warning_uses_the_localized_catalog(language_config, language, error_code):
    language_config(language)
    note = t("error.terminal_write_note")
    expected_detail = t(f"error.{error_code}", note=note)

    error = ApiError(status_code=503, error_code=error_code, note=note)

    assert error.detail == expected_detail, "queue cleanup warning must match the selected locale"
    assert error.error_code == error_code


@pytest.mark.parametrize(("source_detail", "expected_code"), _STATIC_BASE_DETAILS + _DYNAMIC_BASE_DETAILS)
def test_api_errors_are_english_without_cjk(language_config, source_detail, expected_code):
    language_config("en")

    error = ApiError(
        status_code=400,
        error_code=expected_code,
        **_sample_error_values(expected_code),
    )

    assert error.error_code == expected_code
    assert isinstance(error.detail, str)
    assert _CJK.search(error.detail) is None


@pytest.mark.parametrize(
    "error_code",
    [
        "upload_acceptance_window_expired",
        "upload_content_type",
        "upload_declared_size",
        "upload_declared_size_mismatch",
        "upload_disabled_or_not_ready",
        "upload_history_unavailable",
        "upload_idempotency",
        "upload_idempotency_key",
        "upload_inflight_limit",
        "upload_metadata",
        "upload_not_accepted",
        "upload_observed_size_limit",
        "upload_queue_full",
        "upload_receiver_limit",
        "upload_receipt_not_found",
        "upload_record_not_found",
        "upload_share_closed",
        "upload_share_unavailable",
        "upload_temp_budget",
    ],
)
def test_upload_error_codes_have_english_details(language_config, error_code):
    language_config("en")
    error = ApiError(status_code=400, error_code=error_code, detail="source detail")

    assert error.error_code == error_code
    assert isinstance(error.detail, str)
    assert _CJK.search(error.detail) is None


def test_framework_http_errors_preserve_starlette_detail(language_config):
    language_config("zh-CN")
    app = FastAPI()
    install_api_error_handlers(app)

    @app.get("/api/known")
    async def known():
        return {"ok": True}

    client = TestClient(app)
    not_found = client.get("/api/missing")
    method_not_allowed = client.post("/api/known")

    assert not_found.status_code == 404
    assert not_found.json() == {
        "detail": "Not Found",
        "error_code": "resource_not_found",
    }
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json() == {
        "detail": "Method Not Allowed",
        "error_code": "method_not_allowed",
    }


@pytest.mark.parametrize(
    ("language", "status", "expected_message"),
    [
        ("zh-CN", "queued", "任务排队中"),
        ("zh-CN", "processing", "任务处理中"),
        ("zh-CN", "calibrating", "转录完成，校对/总结生成中"),
        ("zh-CN", "success", "任务已完成"),
        ("zh-CN", "failed", "任务处理失败"),
        ("zh-CN", "unknown", "获取任务状态成功"),
        ("en", "queued", "Task queued"),
        ("en", "processing", "Task is being processed"),
        ("en", "calibrating", "Transcription complete; proofreading/summary generation in progress"),
        ("en", "success", "Task completed"),
        ("en", "failed", "Task failed"),
        ("en", "unknown", "Task status retrieved successfully"),
    ],
)
def test_get_task_status_message_is_localized(language_config, monkeypatch, language, status, expected_message):
    language_config(language)
    from video_transcript_api.api.routes import tasks
    from video_transcript_api.api.services.transcription import verify_token

    cache_manager = MagicMock()
    cache_manager.get_task_by_id.return_value = {
        "task_id": "task-status-message",
        "status": status,
        "error_message": None,
        "view_token": "view-status-message",
    }
    monkeypatch.setattr(tasks, "cache_manager", cache_manager)
    monkeypatch.setattr(tasks, "audit_logger", MagicMock())
    monkeypatch.setattr(tasks, "logger", MagicMock())

    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {
        "user_id": "test-user",
        "api_key": "test-key",
    }
    response = TestClient(app).get("/api/task/task-status-message")

    assert response.status_code == 200
    message = response.json()["message"]
    assert message == expected_message
    if language == "en":
        assert _CJK.search(message) is None


def test_real_authentication_and_transcription_errors_are_localized(language_config, monkeypatch):
    language_config("en")
    from video_transcript_api.api.routes import tasks
    from video_transcript_api.api.services.transcription import user_manager, verify_token

    authenticated_app = FastAPI()
    install_api_error_handlers(authenticated_app)
    authenticated_app.include_router(tasks.router)
    monkeypatch.setattr(user_manager, "validate_token", lambda token: None)
    auth_response = TestClient(authenticated_app).get(
        "/api/task/private-task",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert auth_response.status_code == 401
    assert auth_response.json() == {
        "detail": "The authorization token is invalid",
        "error_code": "authorization_token_invalid",
    }

    cache_manager = MagicMock()
    cache_manager.get_task_by_id.return_value = None
    monkeypatch.setattr(tasks, "cache_manager", cache_manager)
    monkeypatch.setattr(tasks, "audit_logger", MagicMock())
    monkeypatch.setattr(tasks, "logger", MagicMock())
    authenticated_app.dependency_overrides[verify_token] = lambda: {
        "user_id": "test-user",
        "api_key": "test-key",
    }
    missing_task_response = TestClient(authenticated_app).get("/api/task/missing-task")
    assert missing_task_response.status_code == 404
    assert missing_task_response.json() == {
        "detail": "Task not found: missing-task",
        "error_code": "task_id_not_found",
    }

    submission_app = FastAPI()
    install_api_error_handlers(submission_app)
    submission_app.include_router(tasks.router)
    submission_app.dependency_overrides[verify_token] = lambda: {
        "user_id": "test-user",
        "api_key": "test-key",
    }
    invalid_url_response = TestClient(submission_app).post(
        "/api/transcribe",
        json={"url": ""},
    )
    assert invalid_url_response.status_code == 400
    assert invalid_url_response.json() == {
        "detail": "The video URL is required",
        "error_code": "invalid_url",
    }


def test_create_app_registers_the_json_error_handler(language_config):
    language_config("en")
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from video_transcript_api.api.app import create_app

    app = create_app(start_background=False)

    assert app.exception_handlers[StarletteHTTPException].__name__ == "handle_http_error"


_TASK_FAILURE_BASE_DETAILS = [
    ("local_upload_mapping_missing", "本地上传的持久化准入记录或媒体映射缺失", {}),
    ("transcription_failed", "转录任务失败: probe-error", {"failure_label": "", "error": "probe-error"}),
    ("transcription_submit_failed", "提交任务失败: probe-error", {"error": "probe-error"}),
    ("calibrating_status_write_failed", "任务状态写入异常: probe-error", {"error": "probe-error"}),
    ("llm_handoff_queue_failed", "LLM任务加入队列失败: probe-error", {"error": "probe-error"}),
    ("youtube_subtitles_empty", "[youtube-api] subtitles are empty", {}),
    ("youtube_transcript_cache_save_failed", "[youtube-api] 转录结果保存到缓存失败", {}),
    ("media_admission_failed", "媒体准入失败：probe-error", {"error": "probe-error"}),
    ("youtube_api_failed", "YouTube API Server error: [404] probe-error", {"code": 404, "error": "probe-error"}),
    ("youtube_api_unexpected", "YouTube API Server unexpected error: probe-error", {"error": "probe-error"}),
    ("platform_subtitle_cache_save_failed", "保存平台字幕到缓存失败", {}),
    ("download_info_unavailable", "无法获取下载信息: probe-url", {"url": "probe-url"}),
    ("download_file_failed", "下载文件失败: probe-url", {"url": "probe-url"}),
    ("upload_media_duration_unknown", "上传媒体时长未知，拒绝进入转录（media_duration_unknown）", {}),
    ("upload_media_duration_limit", "上传媒体超过允许时长，拒绝进入转录（media_duration_limit）", {}),
    ("funasr_transcript_cache_save_failed", "保存FunASR转录结果到缓存失败", {}),
    ("capswriter_transcript_cache_save_failed", "保存CapsWriter转录结果到缓存失败", {}),
    ("upload_temp_budget_exceeded", "上传转录产生的临时文件超过预算（upload_temp_budget）", {}),
    ("worker_unhandled_error", "UPLOAD_PROCESSING_FAILED: 转录任务异常: probe-error", {"failure_label": "UPLOAD_PROCESSING_FAILED: ", "error": "probe-error"}),
    ("upload_processing_prefix", "UPLOAD_PROCESSING_FAILED: probe reason", {"reason": "probe reason"}),
    ("llm_submit_status_recovered", "LLM 任务提交失败，终态写入曾经失败，已由运行期维护补偿写入", {}),
    ("llm_executor_submit_failed", "提交LLM任务失败: probe-error", {"error": "probe-error"}),
    ("recalibration_failed", "重新校对失败: probe-error", {"error": "probe-error"}),
    ("llm_processing_failed", "LLM处理失败: probe-error", {"error": "probe-error"}),
    ("transcription_queue_rejected", "任务队列已满，提交被拒绝", {}),
    ("transcription_queue_failed", "任务加入队列失败: probe-error", {"error": "probe-error"}),
    ("recalibration_input_formatting_failed", "转录数据格式化失败: probe-error", {"error": "probe-error"}),
    ("recalibration_queue_rejected", "LLM 队列已满，重新校对提交被拒绝", {}),
    ("recalibration_queue_failed", "任务加入队列失败: probe-error", {"error": "probe-error"}),
    ("resummary_input_formatting_failed", "转录数据格式化失败: probe-error", {"error": "probe-error"}),
    ("resummary_queue_rejected", "LLM 队列已满，重新生成总结提交被拒绝", {}),
    ("resummary_queue_failed", "任务加入队列失败: probe-error", {"error": "probe-error"}),
    ("notes_queue_rejected", "LLM 队列已满，详细笔记提交被拒绝", {}),
    ("notes_queue_failed", "详细笔记任务加入队列失败: probe-error", {"error": "probe-error"}),
]


@pytest.mark.parametrize(("error_code", "expected_zh", "values"), _TASK_FAILURE_BASE_DETAILS)
def test_task_failure_catalog_preserves_base_reason(language_config, error_code, expected_zh, values):
    language_config("zh-CN")
    chinese = t(f"error.task_failure.{error_code}", **values)
    assert chinese == expected_zh

    language_config("en")
    english = t(f"error.task_failure.{error_code}", **values)
    assert isinstance(english, str)
    assert all(str(value) in english for value in values.values() if value)
    assert _CJK.search(english) is None


def test_task_route_failure_writes_use_localized_catalog_keys():
    tasks_path = PROJECT_ROOT / "src" / "video_transcript_api" / "api" / "routes" / "tasks.py"
    tree = ast.parse(tasks_path.read_text(encoding="utf-8"), filename=str(tasks_path))
    expected_codes = {
        "transcription_queue_rejected",
        "transcription_queue_failed",
        "recalibration_input_formatting_failed",
        "recalibration_queue_rejected",
        "recalibration_queue_failed",
        "resummary_input_formatting_failed",
        "resummary_queue_rejected",
        "resummary_queue_failed",
        "notes_queue_rejected",
        "notes_queue_failed",
    }
    observed_codes = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_fail_task_after_creation":
            continue
        reason = node.args[1]
        assert isinstance(reason, ast.Call)
        assert isinstance(reason.func, ast.Name) and reason.func.id == "t"
        assert isinstance(reason.args[0], ast.Constant)
        key = reason.args[0].value
        assert key.startswith("error.task_failure.")
        observed_codes.add(key.removeprefix("error.task_failure."))

    assert observed_codes == expected_codes


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_manual_history_status_422_has_error_code(language_config, monkeypatch, language):
    language_config(language)
    from video_transcript_api.api.routes import audit
    from video_transcript_api.api.services.transcription import verify_token

    monkeypatch.setattr(audit, "audit_logger", MagicMock())
    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(audit.router)
    app.dependency_overrides[verify_token] = lambda: {"user_id": "test-user", "api_key": "test-key"}

    response = TestClient(app).get("/api/audit/history?source=upload&status=bogus")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "status must be one of: success, failed, all",
        "error_code": "invalid_history_status",
    }


def test_upload_failure_prefix_preserves_the_localized_reason(language_config):
    language_config("zh-CN")
    chinese_reason = t("error.task_failure.upload_media_duration_unknown")
    chinese = t("error.task_failure.upload_processing_prefix", reason=chinese_reason)
    assert chinese == (
        "UPLOAD_PROCESSING_FAILED: 上传媒体时长未知，拒绝进入转录（media_duration_unknown）"
    )

    language_config("en")
    english_reason = t("error.task_failure.upload_media_duration_unknown")
    english = t("error.task_failure.upload_processing_prefix", reason=english_reason)
    assert english == (
        "UPLOAD_PROCESSING_FAILED: Upload media duration is unknown; refusing transcription "
        "(media_duration_unknown)"
    )
    assert _CJK.search(english) is None


@pytest.mark.parametrize(
    ("language", "expected_error"),
    [
        ("zh-CN", "LLM任务加入队列失败: provider unavailable"),
        ("en", "Failed to enqueue the LLM task: provider unavailable"),
    ],
)
def test_specific_task_failure_survives_write_and_get_task(language_config, monkeypatch, language, expected_error):
    language_config(language)
    from video_transcript_api.api.routes import tasks
    from video_transcript_api.api.services import transcription
    from video_transcript_api.api.services.transcription import verify_token

    cache_manager = MagicMock()
    cache_manager.update_task_status.return_value = True
    queue = MagicMock()
    queue.put.side_effect = RuntimeError("provider unavailable")
    terminal_write = {}

    def capture_terminal_write(*args, **kwargs):
        terminal_write.update(kwargs)
        return True

    monkeypatch.setattr(transcription, "cache_manager", cache_manager)
    monkeypatch.setattr(transcription, "llm_task_queue", queue)
    monkeypatch.setattr(
        transcription, "finalize_terminal_status_and_notify", capture_terminal_write
    )
    result = transcription._handoff_to_llm_stage(
        "task-1",
        {"platform": "youtube"},
        calibrating_status_kwargs={},
        task_notifier=object(),
        log_context="unit-test",
        observability={},
    )

    assert result["status"] == "failed"
    assert terminal_write["error_message"] == expected_error
    assert terminal_write["notify_error"] == "【LLM任务加入队列失败】provider unavailable"
    cache_manager.get_task_by_id.return_value = {
        "task_id": "task-1",
        "status": "failed",
        "error_message": terminal_write["error_message"],
        "view_token": "view-1",
    }
    monkeypatch.setattr(tasks, "cache_manager", cache_manager)
    monkeypatch.setattr(tasks, "audit_logger", MagicMock())
    monkeypatch.setattr(tasks, "logger", MagicMock())

    app = FastAPI()
    install_api_error_handlers(app)
    app.include_router(tasks.router)
    app.dependency_overrides[verify_token] = lambda: {
        "user_id": "test-user",
        "api_key": "test-key",
    }
    response = TestClient(app).get("/api/task/task-1")

    assert response.status_code == 200
    returned_error = response.json()["data"]["error"]
    assert returned_error == expected_error
    assert "provider unavailable" in returned_error
    if language == "en":
        assert _CJK.search("Failed to enqueue the LLM task: ") is None
@pytest.mark.parametrize(
    ("language", "expected_error"),
    [
        ("zh-CN", "提交LLM任务失败: provider unavailable"),
        ("en", "Failed to submit the LLM task: provider unavailable"),
    ],
)
def test_task_status_error_matches_inline_and_outbox_notification(
    language_config, tmp_path, request, language, expected_error
):
    language_config(language)
    from video_transcript_api.api.services.terminal_status import (
        deliver_pending_terminal_notifications,
        finalize_terminal_status_and_notify,
    )
    from video_transcript_api.cache.cache_manager import CacheManager
    from video_transcript_api.utils.task_status import TaskStatus

    cache_manager = CacheManager(cache_dir=str(tmp_path / "cache"))
    request.addfinalizer(cache_manager.close)
    task_id = cache_manager.create_task(url="https://example.com/probe")["task_id"]
    assert cache_manager.update_task_status(task_id, TaskStatus.PROCESSING)
    error_message = t(
        "error.task_failure.llm_executor_submit_failed",
        error="provider unavailable",
    )
    assert error_message == expected_error

    router = MagicMock()
    router.notify_task_status.side_effect = [{"wechat": False}, {"wechat": True}]
    with patch(
        "video_transcript_api.api.services.terminal_status._resolve_view_url",
        return_value=None,
    ):
        assert finalize_terminal_status_and_notify(
            task_id,
            TaskStatus.FAILED,
            error_message=error_message,
            cache_manager=cache_manager,
            router=router,
        )
        inline_error = router.notify_task_status.call_args_list[0].kwargs["error"]
        with cache_manager._get_cursor() as cursor:
            cursor.execute(
                "SELECT error_message FROM task_terminal_notifications WHERE task_id = ?",
                (task_id,),
            )
            persisted_error = cursor.fetchone()["error_message"]
        assert deliver_pending_terminal_notifications(cache_manager, router=router) == 1

    retry_error = router.notify_task_status.call_args_list[1].kwargs["error"]
    retry_reason = retry_error.split("\n", 1)[0]
    assert persisted_error == expected_error
    assert inline_error == expected_error
    assert retry_reason == expected_error
    if language == "en":
        assert inline_error == retry_reason
        assert _CJK.search(inline_error) is None
    else:
        assert inline_error == retry_reason == "提交LLM任务失败: provider unavailable"


def test_localized_task_status_failure_calls_do_not_override_notification_error():
    production_files = {
        "llm_ops.py": "llm_executor_submit_failed",
        "context.py": "llm_submit_status_recovered",
    }
    services_dir = PROJECT_ROOT / "src" / "video_transcript_api" / "api" / "services"
    for filename, error_code in production_files.items():
        path = (
            PROJECT_ROOT / "src" / "video_transcript_api" / "api" / filename
            if filename == "context.py"
            else services_dir / filename
        )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        matching_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "finalize_terminal_status_and_notify":
                continue
            error_message = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "error_message"),
                None,
            )
            error_keys = {
                value.value
                for value in ast.walk(error_message) if isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            } if error_message is not None else set()
            if f"error.task_failure.{error_code}" in error_keys:
                assert "notify_error" not in {keyword.arg for keyword in node.keywords}
                matching_calls.append(node)
        assert len(matching_calls) == 1

    transcription_path = services_dir / "transcription.py"
    transcription_tree = ast.parse(
        transcription_path.read_text(encoding="utf-8"), filename=str(transcription_path)
    )
    fail_task = next(
        node for node in ast.walk(transcription_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_fail_task_and_notify"
    )
    matching_calls = [
        node for node in ast.walk(fail_task)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "finalize_terminal_status_and_notify"
    ]
    assert len(matching_calls) == 1
    assert "notify_error" not in {keyword.arg for keyword in matching_calls[0].keywords}


def test_error_catalog_keys_exist_for_each_language():
    catalogs = {
        language: json.loads(
            (LOCALES_DIR / f"{language}.json").read_text(encoding="utf-8")
        )
        for language in SUPPORTED_LANGUAGES
    }
    error_keys = {
        f"error.{code}"
        for _, code in _STATIC_BASE_DETAILS + _DYNAMIC_BASE_DETAILS
    }
    error_keys.update(
        {
            f"error.{code}"
            for code in [
                "upload_acceptance_window_expired",
                "upload_content_type",
                "upload_declared_size",
                "upload_declared_size_mismatch",
                "upload_disabled_or_not_ready",
                "upload_idempotency",
                "upload_idempotency_key",
                "upload_inflight_limit",
                "upload_metadata",
                "upload_observed_size_limit",
                "upload_queue_full",
                "upload_receiver_limit",
                "upload_temp_budget",
            ]
        }
    )
    error_keys.update(
        {
            f"error.task_failure.{code}"
            for code, _detail, _values in _TASK_FAILURE_BASE_DETAILS
        }
    )
    error_keys.update(
        {
            "error.task_failed",
            "error.terminal_write_note",
        }
    )
    for language, catalog in catalogs.items():
        catalog_error_keys = {key for key in catalog if key.startswith("error.")}
        assert catalog_error_keys == error_keys
    task_status_keys = {
        "api.task_status.calibrating",
        "api.task_status.failed",
        "api.task_status.processing",
        "api.task_status.query_succeeded",
        "api.task_status.queued",
        "api.task_status.success",
    }
    for catalog in catalogs.values():
        assert task_status_keys <= set(catalog)
    assert set(catalogs["zh-CN"]) == set(catalogs["en"])
    assert all(not _CJK.search(value) for value in catalogs["en"].values())
