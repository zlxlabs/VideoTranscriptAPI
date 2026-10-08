"""
临时文件管理器

提供统一的临时文件管理机制，确保临时文件不会泄漏。
支持：
- 自动跟踪所有创建的临时文件
- 程序退出时自动清理
- 启动时清理旧的临时文件
- 上下文管理器支持
- 跨平台兼容性
"""

import os
import time
import shutil
import tempfile
import signal
import atexit
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set
from contextlib import contextmanager

from .logging import setup_logger

logger = setup_logger("tempfile_manager")


class TempFileManager:
    """
    统一的临时文件管理器

    负责跟踪和管理所有临时文件，确保不会发生文件泄漏。
    支持程序退出时的自动清理和启动时的旧文件清理。
    """

    def __init__(self, base_dir: str = "./data/temp", retention_hours: float = 24):
        """
        初始化临时文件管理器

        Args:
            base_dir: 临时文件基础目录
            retention_hours: 兜底扫描的默认保留时长（小时），超龄的非活跃文件会被清理
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.retention_hours = retention_hours

        # 跟踪所有创建的临时文件和目录（全局兜底，进程退出时清理）
        self.temp_files: List[Path] = []

        # 任务级登记：task_id -> 任务专属目录（data/temp/task_<id>/）
        # 同时充当"清理"与"在途保护"的单一数据源（见 D2/D10 评审决议）。
        self._task_dirs: Dict[str, Path] = {}
        # 当前正在处理（下载/转录中）的任务集合，受保护不被扫描误删
        self._active_tasks: Set[str] = set()
        # 保护上述结构与节流时间戳的可重入锁（ThreadPool 并发）
        self._lock = threading.RLock()
        # 线程本地：当前线程正在处理的 task_id，用于把新建临时文件自动归属到任务目录
        self._current = threading.local()
        # 惰性扫描节流时间戳
        self._last_sweep_ts: float = 0.0

        # 设置信号处理和清理钩子
        self._setup_cleanup_handlers()

        logger.info(
            f"临时文件管理器已初始化，基础目录: {self.base_dir}，"
            f"保留时长: {self.retention_hours}h"
        )

    # ------------------------------------------------------------------
    # 任务级生命周期：当前任务绑定 / 任务目录 / 活跃标记
    # ------------------------------------------------------------------

    def set_current_task(self, task_id: str) -> None:
        """绑定当前线程的 task_id；之后 create_temp_file/dir 会落到该任务目录。"""
        self._current.task_id = task_id

    def clear_current_task(self) -> None:
        """清除当前线程的 task_id 绑定（任务结束时必须调用，避免线程复用串号）。"""
        self._current.task_id = None

    def get_current_task(self) -> Optional[str]:
        """获取当前线程绑定的 task_id（未绑定返回 None）。"""
        return getattr(self._current, "task_id", None)

    def create_task_dir(self, task_id: str) -> Path:
        """创建并登记任务专属目录 data/temp/task_<id>/。"""
        with self._lock:
            d = self.base_dir / f"task_{task_id}"
            d.mkdir(parents=True, exist_ok=True)
            self._task_dirs[task_id] = d
            logger.debug(f"创建任务临时目录: {d}")
            return d

    def get_task_dir(self, task_id: str) -> Optional[Path]:
        """获取已登记的任务目录（未登记返回 None）。"""
        with self._lock:
            return self._task_dirs.get(task_id)

    def get_current_task_dir(self) -> Path:
        """返回当前任务的专属目录（必要时惰性创建）；无当前任务时回退到基础目录。"""
        task_id = self.get_current_task()
        if task_id is None:
            return self.base_dir
        with self._lock:
            d = self._task_dirs.get(task_id)
            if d is None:
                d = self.create_task_dir(task_id)
            return d

    def mark_active(self, task_id: str) -> None:
        """标记任务为活跃（在途），其目录不会被扫描清理。"""
        with self._lock:
            self._active_tasks.add(task_id)

    def mark_done(self, task_id: str) -> None:
        """取消活跃标记（任务结束）。"""
        with self._lock:
            self._active_tasks.discard(task_id)

    def is_active(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._active_tasks

    def clean_up_task(self, task_id: str) -> int:
        """
        清理某个任务的全部临时文件：rmtree 任务目录，并从所有跟踪结构中一致移除。

        Returns:
            int: 释放的字节数
        """
        with self._lock:
            d = self._task_dirs.pop(task_id, None)
            self._active_tasks.discard(task_id)

        freed = 0
        if d is not None and d.exists():
            freed = self._path_size(d)
            shutil.rmtree(d)
            logger.info(
                f"任务临时文件已清理: task_{task_id} "
                f"(释放 {freed / 1024 / 1024:.2f} MB)"
            )

        # 从全局 tracked 列表里移除该任务目录下的条目，避免列表无限增长
        if d is not None:
            with self._lock:
                self.temp_files = [
                    p for p in self.temp_files if not self._is_within(p, d)
                ]
        return freed

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _path_size(path: Path) -> int:
        """计算文件或目录的总字节数（不存在返回 0）。"""
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total = 0
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    @staticmethod
    def _latest_mtime(path: Path) -> float:
        """返回路径自身及其内容中最新的 mtime（用于判断目录是否仍在被写入）。"""
        try:
            latest = path.stat().st_mtime
        except OSError:
            return 0.0
        if path.is_dir():
            for f in path.rglob("*"):
                try:
                    latest = max(latest, f.stat().st_mtime)
                except OSError:
                    pass
        return latest

    def maybe_sweep(self, min_interval_seconds: float = 1800) -> int:
        """
        节流的惰性扫描：距上次扫描不足 min_interval_seconds 则跳过（返回 -1）。

        节流时间戳在锁内抢先更新，保证并发任务启动时只有一个真正执行扫描。

        Returns:
            int: 清理的条目数；被节流时返回 -1
        """
        now = time.time()
        with self._lock:
            if now - self._last_sweep_ts < min_interval_seconds:
                return -1
            self._last_sweep_ts = now
        return self.clean_up_old_files(silent=True)

    def create_temp_file(self, suffix: str = None, prefix: str = None) -> Path:
        """
        创建临时文件并自动跟踪

        Args:
            suffix: 文件后缀（如 '.mp3'）
            prefix: 文件前缀

        Returns:
            Path: 临时文件路径

        Examples:
            >>> manager = TempFileManager()
            >>> temp_file = manager.create_temp_file(suffix='.txt')
            >>> temp_file.write_text('content')
            >>> # 文件会在程序退出时自动删除
        """
        try:
            target_dir = self.get_current_task_dir()
            temp_file = tempfile.NamedTemporaryFile(
                dir=target_dir,
                suffix=suffix,
                prefix=prefix,
                delete=False,  # 手动管理删除
            )
            temp_path = Path(temp_file.name)
            self.temp_files.append(temp_path)
            logger.debug(f"创建临时文件: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"创建临时文件失败: {e}")
            raise

    def create_temp_dir(self, prefix: str = None) -> Path:
        """
        创建临时目录并自动跟踪

        Args:
            prefix: 目录前缀

        Returns:
            Path: 临时目录路径

        Examples:
            >>> manager = TempFileManager()
            >>> temp_dir = manager.create_temp_dir(prefix='download_')
            >>> # 目录会在程序退出时自动删除
        """
        try:
            target_dir = self.get_current_task_dir()
            temp_dir = tempfile.mkdtemp(dir=target_dir, prefix=prefix)
            temp_path = Path(temp_dir)
            self.temp_files.append(temp_path)
            logger.debug(f"创建临时目录: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"创建临时目录失败: {e}")
            raise

    def track_file(self, file_path: Path) -> None:
        """
        手动跟踪一个已存在的文件/目录

        Args:
            file_path: 文件或目录路径

        Use Cases:
            - 跟踪由外部工具创建的临时文件
            - 跟踪下载的文件
        """
        file_path = Path(file_path)
        if file_path not in self.temp_files:
            self.temp_files.append(file_path)
            logger.debug(f"手动跟踪文件: {file_path}")

    def untrack_file(self, file_path: Path) -> None:
        """
        取消跟踪文件/目录（不会被自动清理）

        Args:
            file_path: 文件或目录路径

        Use Cases:
            - 文件已移动到缓存目录
            - 文件需要持久化保存
        """
        file_path = Path(file_path)
        if file_path in self.temp_files:
            self.temp_files.remove(file_path)
            logger.debug(f"取消跟踪文件: {file_path}")

    def clean_up(self, silent: bool = False) -> int:
        """
        清理所有跟踪的临时文件和目录

        Args:
            silent: 是否静默模式（不记录日志）

        Returns:
            int: 成功清理的文件数量
        """
        cleaned_count = 0

        for path in self.temp_files[:]:  # 使用副本进行迭代
            try:
                if path.exists():
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                    cleaned_count += 1
                    if not silent:
                        logger.debug(f"已清理: {path}")
                # 从跟踪列表中移除
                self.temp_files.remove(path)
            except Exception as e:
                if not silent:
                    logger.warning(f"清理临时文件失败: {path}, 错误: {e}")

        if not silent and cleaned_count > 0:
            logger.info(f"临时文件清理完成，共清理 {cleaned_count} 个文件/目录")

        return cleaned_count

    def clean_up_old_files(
        self, hours: Optional[float] = None, silent: bool = False
    ) -> int:
        """
        兜底扫描：清理 temp 顶层中超龄且非活跃的孤儿文件/任务目录。

        判定规则（D4 在途保护）：某条目被删除 ⟺
            它不属于任何活跃任务（task_<id> 中 id 不在活跃集）
            且 它的最新 mtime 早于 now - hours。
        启动时无活跃任务 → 崩溃残留的孤儿目录被清；
        运行/关闭期活跃任务的目录受保护，不会被误删。

        Args:
            hours: 保留时长（小时）。None 表示使用构造时配置的 retention_hours。
                   传 0 表示"清理所有非活跃条目"（关闭时使用）。
            silent: 是否静默模式

        Returns:
            int: 清理的顶层条目数量
        """
        if hours is None:
            hours = self.retention_hours
        cleaned_count = 0
        freed_bytes = 0
        cutoff_time = time.time() - hours * 3600

        if not self.base_dir.exists():
            return 0

        with self._lock:
            active = set(self._active_tasks)

        # 只遍历顶层条目：松散文件，以及 task_<id>/ 目录
        for item in self.base_dir.iterdir():
            try:
                # 活跃任务目录：在途保护，跳过
                if item.is_dir() and item.name.startswith("task_"):
                    task_id = item.name[len("task_"):]
                    if task_id in active:
                        continue

                if self._latest_mtime(item) < cutoff_time:
                    size = self._path_size(item)
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    cleaned_count += 1
                    freed_bytes += size
                    if not silent:
                        logger.debug(f"清理超龄临时项: {item}")
            except Exception as e:
                if not silent:
                    logger.warning(f"清理超龄临时项失败: {item}, 错误: {e}")

        if not silent and cleaned_count > 0:
            logger.info(
                f"兜底扫描完成：清理了 {cleaned_count} 个超龄项 "
                f"(保留阈值 {hours}h，释放 {freed_bytes / 1024 / 1024:.2f} MB)"
            )

        return cleaned_count

    def get_temp_dir(self) -> Path:
        """
        获取临时文件基础目录

        Returns:
            Path: 临时目录路径
        """
        return self.base_dir

    def get_stats(self) -> dict:
        """
        获取临时文件统计信息

        Returns:
            dict: 统计信息
        """
        tracked_count = len(self.temp_files)
        tracked_size = 0

        for path in self.temp_files:
            if path.exists():
                if path.is_file():
                    tracked_size += path.stat().st_size
                elif path.is_dir():
                    for file in path.rglob("*"):
                        if file.is_file():
                            tracked_size += file.stat().st_size

        return {
            "base_dir": str(self.base_dir),
            "tracked_count": tracked_count,
            "tracked_size_mb": round(tracked_size / 1024 / 1024, 2),
            "temp_files_count": len([f for f in self.temp_files if f.is_file()]),
            "temp_dirs_count": len([d for d in self.temp_files if d.is_dir()]),
        }

    def _setup_cleanup_handlers(self) -> None:
        """设置自动清理处理程序"""
        # 程序正常退出时清理
        atexit.register(self._atexit_cleanup)

        # 捕获退出信号（仅在主进程中）
        if os.getpid() == os.getppid():
            try:
                signal.signal(signal.SIGTERM, self._signal_handler)
                signal.signal(signal.SIGINT, self._signal_handler)
            except Exception as e:
                logger.warning(f"设置信号处理失败: {e}")

    def _atexit_cleanup(self) -> None:
        """atexit 清理回调"""
        try:
            self.clean_up(silent=True)
        except Exception as e:
            # atexit 中不能记录日志，静默处理
            pass

    def _signal_handler(self, signum, frame) -> None:
        """信号处理回调"""
        try:
            logger.info(f"收到退出信号 {signum}，清理临时文件...")
            self.clean_up()
        except Exception as e:
            logger.error(f"信号处理失败: {e}")
        finally:
            # 恢复默认信号处理并退出
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    @contextmanager
    def temp_directory(self, prefix: str = None):
        """
        上下文管理器：创建临时目录并在退出时清理

        Args:
            prefix: 目录前缀

        Yields:
            Path: 临时目录路径

        Examples:
            >>> manager = TempFileManager()
            >>> with manager.temp_directory(prefix='work_') as temp_dir:
            ...     # 使用临时目录
            ...     temp_file = temp_dir / "test.txt"
            ...     temp_file.write_text('content')
            ... # 退出时自动清理
        """
        temp_dir = self.create_temp_dir(prefix=prefix)
        try:
            yield temp_dir
        finally:
            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                self.untrack_file(temp_dir)
            except Exception as e:
                logger.warning(f"清理临时目录失败: {temp_dir}, 错误: {e}")

    @contextmanager
    def temp_file(self, suffix: str = None, prefix: str = None):
        """
        上下文管理器：创建临时文件并在退出时清理

        Args:
            suffix: 文件后缀
            prefix: 文件前缀

        Yields:
            Path: 临时文件路径

        Examples:
            >>> manager = TempFileManager()
            >>> with manager.temp_file(suffix='.txt') as temp_file:
            ...     temp_file.write_text('content')
            ... # 退出时自动清理
        """
        temp_file = self.create_temp_file(suffix=suffix, prefix=prefix)
        try:
            yield temp_file
        finally:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                self.untrack_file(temp_file)
            except Exception as e:
                logger.warning(f"清理临时文件失败: {temp_file}, 错误: {e}")

    def __enter__(self):
        """上下文管理器支持"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出时清理"""
        self.clean_up()

    def __del__(self):
        """析构时清理"""
        try:
            self.clean_up(silent=True)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 全局共享单例
#
# 下载层（downloaders）与 API 层（api）都从这里取同一个实例，保证"清理"与
# "在途保护"看的是同一张登记表（见 D3 评审决议：单例下沉到 utils 层）。
# ----------------------------------------------------------------------------

_shared_manager: Optional["TempFileManager"] = None
_shared_lock = threading.Lock()


def get_shared_temp_manager() -> "TempFileManager":
    """获取全局共享的 TempFileManager 单例（按 storage 配置初始化）。"""
    global _shared_manager
    if _shared_manager is None:
        with _shared_lock:
            if _shared_manager is None:
                from .logging import load_config

                config = load_config()
                storage = config.get("storage", {})
                temp_dir = storage.get("temp_dir", "./data/temp")
                retention = storage.get("temp_retention_hours", 24)
                _shared_manager = TempFileManager(temp_dir, retention_hours=retention)
    return _shared_manager
