"""
多用户功能测试模块

测试用户管理、鉴权和审计功能。
"""

import os
import sys
import tempfile
import json
import unittest
from unittest.mock import patch, MagicMock

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from video_transcript_api.utils.user_manager import UserManager
from video_transcript_api.utils.audit_logger import AuditLogger


class TestMultiUser(unittest.TestCase):
    """多用户功能测试类"""
    
    def setUp(self):
        """测试前的准备工作"""
        # 创建临时用户配置文件
        self.temp_dir = tempfile.mkdtemp()
        self.users_config_path = os.path.join(self.temp_dir, "users.json")
        self.audit_db_path = os.path.join(self.temp_dir, "audit.db")
        
        # 创建测试用户配置
        self.test_users_config = {
            "users": {
                "sk-test001-abcdefghij": {
                    "user_id": "test_user_001",
                    "name": "测试用户001",
                    "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test1",
                    "created_at": "2025-01-01T00:00:00Z",
                    "enabled": True
                },
                "sk-test002-klmnopqrst": {
                    "user_id": "test_user_002",
                    "name": "测试用户002",
                    "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test2",
                    "created_at": "2025-01-01T00:00:00Z",
                    "enabled": True
                },
                "sk-test003-uvwxyz1234": {
                    "user_id": "test_user_003",
                    "name": "已禁用用户",
                    "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test3",
                    "created_at": "2025-01-01T00:00:00Z",
                    "enabled": False
                }
            }
        }
        
        # 写入用户配置文件
        with open(self.users_config_path, 'w', encoding='utf-8') as f:
            json.dump(self.test_users_config, f, indent=2, ensure_ascii=False)
        
        # 创建回退配置
        self.fallback_config = {
            "api": {
                "auth_token": "legacy-token-123456"
            },
            "wechat": {
                "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=legacy"
            }
        }
    
    def tearDown(self):
        """测试后的清理工作"""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_user_manager_initialization(self):
        """测试用户管理器初始化"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 验证多用户模式
        self.assertTrue(user_manager.is_multi_user_mode())
        self.assertEqual(user_manager.get_user_count(), 3)
    
    def test_valid_token_authentication(self):
        """测试有效令牌认证"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 测试有效令牌
        user_info = user_manager.validate_token("sk-test001-abcdefghij")
        self.assertIsNotNone(user_info)
        self.assertEqual(user_info["user_id"], "test_user_001")
        self.assertEqual(user_info["name"], "测试用户001")
        self.assertTrue(user_info["enabled"])
        
        # 测试启用状态的用户
        user_info2 = user_manager.validate_token("sk-test002-klmnopqrst")
        self.assertIsNotNone(user_info2)
        self.assertEqual(user_info2["user_id"], "test_user_002")
    
    def test_disabled_user_authentication(self):
        """测试已禁用用户认证"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 测试已禁用用户
        user_info = user_manager.validate_token("sk-test003-uvwxyz1234")
        self.assertIsNone(user_info)
    
    def test_invalid_token_authentication(self):
        """测试无效令牌认证"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 测试无效令牌
        user_info = user_manager.validate_token("invalid-token")
        self.assertIsNone(user_info)
    
    def test_fallback_token_authentication(self):
        """测试回退令牌认证"""
        # 测试没有用户配置文件的情况
        non_existent_path = os.path.join(self.temp_dir, "non_existent_users.json")
        user_manager = UserManager(
            users_config_path=non_existent_path,
            fallback_config=self.fallback_config
        )
        
        # 验证单token回退模式
        self.assertFalse(user_manager.is_multi_user_mode())
        self.assertEqual(user_manager.get_user_count(), 1)
        
        # 测试回退令牌
        user_info = user_manager.validate_token("legacy-token-123456")
        self.assertIsNotNone(user_info)
        self.assertEqual(user_info["user_id"], "legacy_user")
        self.assertTrue(user_info["is_legacy"])
    
    def test_get_user_webhook(self):
        """测试获取用户webhook"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 测试获取用户webhook
        webhook = user_manager.get_user_webhook("sk-test001-abcdefghij")
        self.assertEqual(webhook, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test1")
        
        # 测试无效令牌
        webhook = user_manager.get_user_webhook("invalid-token")
        self.assertIsNone(webhook)
    
    def test_audit_logger_initialization(self):
        """测试审计日志记录器初始化"""
        audit_logger = AuditLogger(db_path=self.audit_db_path)
        
        # 验证数据库文件创建
        self.assertTrue(os.path.exists(self.audit_db_path))
    
    def test_audit_log_api_call(self):
        """测试API调用日志记录"""
        audit_logger = AuditLogger(db_path=self.audit_db_path)
        
        # 记录API调用
        success = audit_logger.log_api_call(
            api_key="sk-test001-abcdefghij",
            user_id="test_user_001",
            endpoint="/api/transcribe",
            video_url="https://example.com/video",
            processing_time_ms=1500,
            status_code=202,
            task_id="task_123",
            user_agent="TestAgent/1.0",
            remote_ip="192.168.1.100"
        )
        
        self.assertTrue(success)
    
    def test_audit_get_user_stats(self):
        """测试获取用户统计"""
        audit_logger = AuditLogger(db_path=self.audit_db_path)
        
        # 记录一些测试数据
        for i in range(5):
            audit_logger.log_api_call(
                api_key="sk-test001-abcdefghij",
                user_id="test_user_001",
                endpoint="/api/transcribe",
                status_code=200
            )
        
        # 获取用户统计
        stats = audit_logger.get_user_stats("test_user_001", 30)
        
        self.assertEqual(stats["user_id"], "test_user_001")
        self.assertEqual(stats["total_calls"], 5)
        self.assertGreaterEqual(stats["active_days"], 1)
    
    def test_audit_get_recent_calls(self):
        """测试获取最近调用记录"""
        audit_logger = AuditLogger(db_path=self.audit_db_path)
        
        # 记录测试数据
        audit_logger.log_api_call(
            api_key="sk-test001-abcdefghij",
            user_id="test_user_001",
            endpoint="/api/transcribe",
            video_url="https://example.com/video1",
            status_code=200
        )
        
        audit_logger.log_api_call(
            api_key="sk-test002-klmnopqrst",
            user_id="test_user_002",
            endpoint="/api/task/123",
            status_code=200
        )
        
        # 获取指定用户的记录
        calls = audit_logger.get_recent_calls("test_user_001", 10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["user_id"], "test_user_001")
        self.assertEqual(calls[0]["endpoint"], "/api/transcribe")
        
        # 获取所有用户的记录
        all_calls = audit_logger.get_recent_calls(None, 10)
        self.assertEqual(len(all_calls), 2)
    
    def test_api_key_masking(self):
        """测试API密钥脱敏"""
        user_manager = UserManager(
            users_config_path=self.users_config_path,
            fallback_config=self.fallback_config
        )
        
        # 测试正常长度的密钥
        masked = user_manager._mask_api_key("sk-test001-abcdefghij")
        # API密钥长度为22，前4后4，中间14个*
        self.assertEqual(masked, "sk-t*************ghij")
        
        # 测试短密钥
        masked_short = user_manager._mask_api_key("short")
        self.assertEqual(masked_short, "****")
        
        # 测试空密钥
        masked_empty = user_manager._mask_api_key("")
        self.assertEqual(masked_empty, "****")


def run_tests():
    """运行所有测试"""
    unittest.main()


if __name__ == "__main__":
    run_tests()