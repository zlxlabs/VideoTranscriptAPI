"""
用户配置管理模块

提供多用户配置的加载、验证和管理功能。
"""

import os
import json
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
from ..logging import setup_logger

logger = setup_logger("user_manager")


class UserManager:
    """用户配置管理器"""
    
    def __init__(self, users_config_path: str = None, fallback_config: Dict[str, Any] = None):
        """
        初始化用户管理器
        
        Args:
            users_config_path: 用户配置文件路径，默认为 config/users.json
            fallback_config: 回退配置（单token模式）
        """
        if users_config_path is None:
            # 默认用户配置文件路径
            project_root = Path(__file__).resolve().parents[4]
            users_config_path = project_root / "config" / "users.json"
        
        self.users_config_path = str(users_config_path)
        self.fallback_config = fallback_config or {}
        self._users_data = {}
        self._lock = threading.Lock()
        self._load_users_config()
        
        logger.info(f"用户管理器初始化完成，配置文件: {self.users_config_path}")
    
    def _load_users_config(self):
        """加载用户配置文件"""
        with self._lock:
            try:
                if os.path.exists(self.users_config_path):
                    with open(self.users_config_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                        self._users_data = config_data.get("users", {})
                    
                    logger.info(f"加载用户配置成功，包含 {len(self._users_data)} 个用户")
                    
                    # 验证配置格式
                    for api_key, user_info in self._users_data.items():
                        if not isinstance(user_info, dict):
                            logger.warning(f"用户配置格式错误: {api_key}")
                            continue
                        
                        required_fields = ["user_id", "name"]
                        for field in required_fields:
                            if field not in user_info:
                                logger.warning(f"用户配置缺少必需字段 {field}: {api_key}")
                else:
                    logger.info(f"用户配置文件不存在: {self.users_config_path}，将使用单token回退模式")
                    self._users_data = {}
                    
            except Exception as e:
                logger.error(f"加载用户配置失败: {str(e)}，将使用单token回退模式")
                self._users_data = {}
    
    def reload_config(self):
        """重新加载用户配置"""
        logger.info("重新加载用户配置")
        self._load_users_config()
    
    def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        """
        验证API令牌并返回用户信息
        
        Args:
            token: API令牌
            
        Returns:
            dict: 用户信息，如果令牌无效则返回None
        """
        # 首先检查多用户配置
        if self._users_data and token in self._users_data:
            user_info = self._users_data[token].copy()

            # 检查用户是否启用
            if not user_info.get("enabled", True):
                logger.warning(f"用户已禁用: {user_info.get('user_id', 'unknown')}")
                return None

            # 添加API密钥到用户信息中
            user_info["api_key"] = token

            # is_legacy 是内部保留字段，只能由本函数下方的单 token 回退分支
            # 显式赋予 True，代表"系统所有者"这个特殊身份（如
            # /api/audit/stats 用它判定是否可以查看全局 token 用量聚合）。
            # 多用户配置来自外部可编辑的 users.json/config.jsonc，若某个
            # 租户的配置项恰好也写了这个字段名（历史遗留或误操作），上面
            # 的 .copy() 会原样透传，让一个普通租户意外获得所有者视角的
            # 权限——强制覆盖为 False，确保这个字段的语义只能由代码本身
            # 赋予，不受外部配置内容左右（ci-gate review）。
            user_info["is_legacy"] = False

            logger.debug(f"多用户模式验证成功: {user_info.get('user_id')}")
            return user_info
        
        # 如果多用户配置不存在或令牌不匹配，检查单token回退模式
        fallback_token = self.fallback_config.get("api", {}).get("auth_token")
        if fallback_token and token == fallback_token:
            logger.debug("单token回退模式验证成功")
            return {
                "user_id": "legacy_user",
                "name": "Legacy User",
                "api_key": token,
                "wechat_webhook": self.fallback_config.get("wechat", {}).get("webhook"),
                "feishu_webhook": self.fallback_config.get("feishu", {}).get("webhook"),
                "enabled": True,
                "is_legacy": True  # 标记为回退模式用户
            }
        
        logger.warning(f"API令牌验证失败")
        return None
    
    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        根据用户ID获取用户信息
        
        Args:
            user_id: 用户ID
            
        Returns:
            dict: 用户信息，如果用户不存在则返回None
        """
        for api_key, user_info in self._users_data.items():
            if user_info.get("user_id") == user_id:
                result = user_info.copy()
                result["api_key"] = api_key
                # 与 validate_token() 同一致性加固：is_legacy 只能由下方的
                # 单 token 回退分支赋予，不能被多用户配置里同名字段透传。
                result["is_legacy"] = False
                return result
        
        # 检查是否是回退模式用户
        if user_id == "legacy_user":
            fallback_token = self.fallback_config.get("api", {}).get("auth_token")
            if fallback_token:
                return {
                    "user_id": "legacy_user",
                    "name": "Legacy User",
                    "api_key": fallback_token,
                    "wechat_webhook": self.fallback_config.get("wechat", {}).get("webhook"),
                    "feishu_webhook": self.fallback_config.get("feishu", {}).get("webhook"),
                    "enabled": True,
                    "is_legacy": True
                }

        return None

    def get_user_webhook(self, token: str) -> Optional[str]:
        """
        获取用户的企业微信webhook地址
        
        Args:
            token: API令牌
            
        Returns:
            str: webhook地址，如果未配置则返回None
        """
        user_info = self.validate_token(token)
        if user_info:
            return user_info.get("wechat_webhook")
        return None
    
    def list_all_users(self) -> List[Dict[str, Any]]:
        """
        获取所有用户列表（不包含API密钥）
        
        Returns:
            list: 用户信息列表
        """
        users = []
        
        # 添加多用户配置中的用户
        for api_key, user_info in self._users_data.items():
            user_data = user_info.copy()
            user_data["api_key_masked"] = self._mask_api_key(api_key)
            users.append(user_data)
        
        # 添加回退模式用户（如果存在）
        fallback_token = self.fallback_config.get("api", {}).get("auth_token")
        if fallback_token and not self._users_data:  # 只有在没有多用户配置时才显示回退用户
            users.append({
                "user_id": "legacy_user",
                "name": "Legacy User",
                "api_key_masked": self._mask_api_key(fallback_token),
                "wechat_webhook": self.fallback_config.get("wechat", {}).get("webhook"),
                "feishu_webhook": self.fallback_config.get("feishu", {}).get("webhook"),
                "enabled": True,
                "is_legacy": True
            })
        
        return users
    
    def _mask_api_key(self, api_key: str) -> str:
        """
        对API密钥进行脱敏处理
        
        Args:
            api_key: 原始API密钥
            
        Returns:
            str: 脱敏后的API密钥
        """
        if not api_key or len(api_key) < 8:
            return "****"
        
        return f"{api_key[:4]}{'*' * (len(api_key) - 8)}{api_key[-4:]}"
    
    def check_permission(self, user_info: dict, permission: str) -> bool:
        """检查用户是否有指定权限

        legacy 单 token 用户默认拥有所有权限；
        多用户模式下检查 permissions 数组。

        Args:
            user_info: validate_token 返回的用户信息字典
            permission: 需要检查的权限名称（如 "recalibrate"）

        Returns:
            bool: 用户是否拥有该权限
        """
        if user_info.get("is_legacy"):
            return True
        permissions = user_info.get("permissions", [])
        return permission in permissions

    def is_multi_user_mode(self) -> bool:
        """
        检查是否处于多用户模式
        
        Returns:
            bool: True表示多用户模式，False表示单token回退模式
        """
        return len(self._users_data) > 0
    
    def get_user_count(self) -> int:
        """
        获取用户数量
        
        Returns:
            int: 用户数量
        """
        if self.is_multi_user_mode():
            return len(self._users_data)
        else:
            # 单token回退模式算作1个用户
            fallback_token = self.fallback_config.get("api", {}).get("auth_token")
            return 1 if fallback_token else 0


# 全局用户管理器实例
_user_manager = None
_user_manager_lock = threading.Lock()


def get_user_manager(fallback_config: Dict[str, Any] = None) -> UserManager:
    """
    获取全局用户管理器实例（单例模式）
    
    Args:
        fallback_config: 回退配置（仅在首次初始化时使用）
        
    Returns:
        UserManager: 用户管理器实例
    """
    global _user_manager
    
    if _user_manager is None:
        with _user_manager_lock:
            if _user_manager is None:
                _user_manager = UserManager(fallback_config=fallback_config)
    
    return _user_manager
