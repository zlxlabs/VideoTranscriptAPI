import asyncio
import concurrent.futures
import queue
import threading
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any

from fastapi.templating import Jinja2Templates

from ..utils.accounts import get_user_manager as _get_user_manager_impl
from ..utils.cache import CacheManager, MetadataCache
from ..utils.llm import EnhancedLLMProcessor
from ..utils.logging import get_audit_logger as _get_audit_logger_impl
from ..utils.logging import load_config as _load_config_impl
from ..utils.logging import setup_logger

# In-memory task result store shared across routes/workers
_task_results: Dict[str, Dict[str, Any]] = {}

# Lazy initialized runtime resources
_task_queue: asyncio.Queue | None = None
_executor: concurrent.futures.ThreadPoolExecutor | None = None
_llm_task_queue: queue.Queue | None = None
_llm_processing_lock = threading.Lock()
_templates: Jinja2Templates | None = None


@lru_cache
def get_logger():
    """Return the API logger singleton."""
    return setup_logger("api_server")


@lru_cache
def get_config():
    """Load configuration once."""
    return _load_config_impl()


@lru_cache
def get_user_manager():
    """User manager shared across routes."""
    return _get_user_manager_impl(fallback_config=get_config())


@lru_cache
def get_audit_logger():
    """Audit logger singleton."""
    return _get_audit_logger_impl()


@lru_cache
def get_metadata_cache():
    return MetadataCache()


@lru_cache
def get_cache_manager():
    cache_dir = get_config().get("storage", {}).get("cache_dir", "./data/cache")
    return CacheManager(cache_dir)


@lru_cache
def get_enhanced_llm_processor():
    return EnhancedLLMProcessor(get_config())


def get_task_results() -> Dict[str, Dict[str, Any]]:
    """Global task state store."""
    return _task_results


def get_task_queue() -> asyncio.Queue:
    """Create (if needed) and return the transcription task queue."""
    global _task_queue
    if _task_queue is None:
        queue_size = get_config().get("concurrent", {}).get("queue_size", 10)
        _task_queue = asyncio.Queue(queue_size)
    return _task_queue


def get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the shared transcription thread pool."""
    global _executor
    if _executor is None:
        max_workers = get_config().get("concurrent", {}).get("max_workers", 3)
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    return _executor


def get_llm_queue() -> queue.Queue:
    """Queue used for serialized LLM post-processing tasks."""
    global _llm_task_queue
    if _llm_task_queue is None:
        _llm_task_queue = queue.Queue(maxsize=100)
    return _llm_task_queue


def get_llm_processing_lock() -> threading.Lock:
    return _llm_processing_lock


def get_template_dir() -> Path:
    """Return src/web/templates directory path."""
    return Path(__file__).resolve().parents[3] / "web" / "templates"


def get_templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        _templates = Jinja2Templates(directory=str(get_template_dir()))
    return _templates


def get_static_dir() -> Path:
    """Return src/web/static directory path."""
    return Path(__file__).resolve().parents[3] / "web" / "static"
