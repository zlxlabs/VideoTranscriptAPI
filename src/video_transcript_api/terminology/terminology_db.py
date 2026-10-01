"""专有名词库管理模块

提供从 JSON 文件加载专有名词、查询匹配项、生成 LLM prompt 注入文本的功能。
支持默认词库和用户自定义词库。
"""

import json
import threading
from pathlib import Path
from typing import List, Dict, Optional
from ..utils.logging import setup_logger

logger = setup_logger("terminology")


class TerminologyDB:
    """专有名词库

    从 JSON 文件加载专有名词对照表，提供查询和 prompt 注入功能。

    Attributes:
        terms: 已加载的专有名词列表
    """

    def __init__(self, custom_path: Optional[str] = None):
        """初始化专有名词库

        Args:
            custom_path: 用户自定义词库路径，为 None 时仅使用默认词库
        """
        self.terms: List[Dict[str, str]] = []
        self._load_default_terms()
        if custom_path:
            self._load_custom_terms(custom_path)
        logger.info(f"terminology DB initialized with {len(self.terms)} terms")

    def _load_default_terms(self):
        """加载默认专有名词库"""
        default_path = Path(__file__).parent / "data" / "default_terms.json"
        self._load_terms_file(default_path, source="default")

    def _load_custom_terms(self, path: str):
        """加载用户自定义专有名词库

        Args:
            path: 自定义词库文件路径
        """
        custom_path = Path(path)
        if custom_path.exists():
            self._load_terms_file(custom_path, source="custom")
        else:
            logger.warning(f"custom terminology file not found: {path}")

    def _load_terms_file(self, path: Path, source: str = "unknown"):
        """从 JSON 文件加载专有名词

        Args:
            path: JSON 文件路径
            source: 来源标识（用于日志）
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            version = data.get("version", 0)
            terms = data.get("terms", [])

            if not isinstance(terms, list):
                logger.error(f"invalid terms format in {path}: expected list")
                return

            # 验证每个条目的必要字段
            valid_terms = []
            for term in terms:
                if "incorrect" in term and "correct" in term:
                    valid_terms.append({
                        "incorrect": term["incorrect"],
                        "correct": term["correct"],
                        "category": term.get("category", "general"),
                    })
                else:
                    logger.warning(f"skipping invalid term entry: {term}")

            self.terms.extend(valid_terms)
            logger.info(
                f"loaded {len(valid_terms)} terms from {source} (v{version}): {path}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"failed to parse terminology file {path}: {e}")
        except Exception as e:
            logger.error(f"failed to load terminology file {path}: {e}")

    def query(self, text: str) -> List[Dict[str, str]]:
        """查询文本中可能匹配的专有名词

        对文本进行大小写不敏感的匹配，返回匹配到的专有名词列表。

        Args:
            text: 待查询的文本

        Returns:
            list: 匹配到的专有名词列表
        """
        if not text:
            return []

        text_lower = text.lower()
        matched = []
        seen_correct = set()

        for term in self.terms:
            if term["incorrect"].lower() in text_lower:
                if term["correct"] not in seen_correct:
                    matched.append(term)
                    seen_correct.add(term["correct"])

        return matched

    def export_correct_terms(self) -> List[str]:
        """导出词库中的正确写法，供 ASR 热词使用。

        不依赖待转录文本，因此与 ``query`` 的文本匹配语义分开。
        """
        return [
            term["correct"]
            for term in self.terms
            if isinstance(term.get("correct"), str)
        ]

    def format_for_prompt(self, matched_terms: Optional[List[Dict[str, str]]] = None) -> str:
        """将专有名词列表格式化为 LLM prompt 注入文本

        Args:
            matched_terms: 匹配到的专有名词列表，为 None 时返回全量列表

        Returns:
            str: 格式化后的 prompt 文本，无匹配时返回空字符串
        """
        terms_to_format = matched_terms if matched_terms is not None else self.terms

        if not terms_to_format:
            return ""

        # 按类别分组
        by_category: Dict[str, List[Dict[str, str]]] = {}
        for term in terms_to_format:
            cat = term.get("category", "general")
            by_category.setdefault(cat, []).append(term)

        lines = []
        for category, terms in by_category.items():
            corrections = [f"{t['incorrect']} → {t['correct']}" for t in terms]
            lines.append(f"- {category}: {', '.join(corrections)}")

        return "\n".join(lines)

    def format_matched_for_prompt(self, text: str) -> str:
        """查询文本中的匹配项并格式化为 prompt 注入文本

        Args:
            text: 待查询的文本（通常是转录原文或元数据）

        Returns:
            str: 格式化后的 prompt 文本，无匹配时返回空字符串
        """
        matched = self.query(text)
        return self.format_for_prompt(matched)


# 全局单例
_terminology_db: Optional[TerminologyDB] = None
_terminology_lock = threading.Lock()


def get_terminology_db(custom_path: Optional[str] = None) -> TerminologyDB:
    """获取全局专有名词库实例（单例模式）

    Args:
        custom_path: 用户自定义词库路径

    Returns:
        TerminologyDB: 专有名词库实例
    """
    global _terminology_db

    if _terminology_db is None:
        with _terminology_lock:
            if _terminology_db is None:
                _terminology_db = TerminologyDB(custom_path=custom_path)

    return _terminology_db
