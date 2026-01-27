"""测试新架构 LLMCoordinator"""

import unittest
from unittest.mock import Mock, patch
from video_transcript_api.utils.llm import LLMCoordinator


class TestNewArchitecture(unittest.TestCase):
    """测试新架构组件"""

    def setUp(self):
        """设置测试配置"""
        self.config_dict = {
            "llm": {
                "api_key": "test_key",
                "base_url": "http://test.api.com",
                "calibrate_model": "test-model",
                "summary_model": "test-model",
                "key_info_model": "test-model",
                "speaker_model": "test-model",
                "segmentation": {},
                "structured_calibration": {},
            }
        }
        self.cache_dir = "./test_cache_dir"

    def test_coordinator_initialization(self):
        """测试协调器初始化"""
        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        self.assertIsNotNone(coordinator)
        self.assertIsNotNone(coordinator.config)
        self.assertIsNotNone(coordinator.plain_text_processor)
        self.assertIsNotNone(coordinator.speaker_aware_processor)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_coordinator_plain_text_processing(self, mock_call):
        """测试纯文本处理"""
        # 模拟LLM响应
        mock_response = Mock()
        mock_response.text = "这是校对后的文本。"
        mock_response.structured_output = {
            "names": ["张三"],
            "places": ["北京"],
            "terms": [],
            "brands": [],
            "abbreviations": {}
        }
        mock_call.return_value = mock_response

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        # 测试短文本（不需要分段）
        result = coordinator.process(
            content="这是一段测试文本。",
            title="测试标题",
            author="测试作者",
            description="测试描述",
            platform="test",
            media_id="test_123",
        )

        self.assertIn("calibrated_text", result)
        self.assertIn("key_info", result)
        self.assertIn("stats", result)

    @patch('video_transcript_api.utils.llm.core.llm_client.LLMClient.call')
    def test_coordinator_speaker_aware_processing(self, mock_call):
        """测试说话人感知处理"""
        # 模拟多个LLM响应
        key_info_response = Mock()
        key_info_response.structured_output = {
            "names": ["张三", "李四"],
            "places": ["北京"],
            "terms": [],
            "brands": [],
            "abbreviations": {}
        }

        speaker_response = Mock()
        speaker_response.structured_output = {
            "speaker_mapping": {"spk0": "张三", "spk1": "李四"},
            "confidence": {"spk0": 0.9, "spk1": 0.8},
            "reasoning": "根据对话内容推断"
        }

        calibrate_response = Mock()
        calibrate_response.structured_output = {
            "calibrated_dialogs": [
                {"speaker": "spk0", "text": "你好，我是张三。", "start_time": 0.0},
                {"speaker": "spk1", "text": "你好，我是李四。", "start_time": 2.5},
            ]
        }

        mock_call.side_effect = [key_info_response, speaker_response, calibrate_response]

        coordinator = LLMCoordinator(
            config_dict=self.config_dict,
            cache_dir=self.cache_dir,
        )

        # 测试对话列表处理
        dialogs = [
            {"speaker": "spk0", "text": "你好我是张三", "start_time": 0.0},
            {"speaker": "spk1", "text": "你好我是李四", "start_time": 2.5},
        ]

        result = coordinator.process(
            content=dialogs,
            title="测试对话",
            author="测试作者",
            platform="test",
            media_id="test_456",
        )

        self.assertIn("calibrated_text", result)
        self.assertIn("structured_data", result)
        self.assertIn("key_info", result)
        self.assertIn("stats", result)
        self.assertIn("dialogs", result["structured_data"])
        self.assertIn("speaker_mapping", result["structured_data"])

    def test_prompt_function_backward_compatibility(self):
        """测试prompt函数的向后兼容性"""
        from video_transcript_api.utils.llm.prompts import build_structured_calibrate_user_prompt

        # 测试旧版调用方式
        input_data = {
            "dialogs": [
                {"speaker": "spk0", "text": "你好", "start_time": "00:00:00"},
                {"speaker": "spk1", "text": "你好", "start_time": "00:00:02"},
            ]
        }

        old_style_prompt = build_structured_calibrate_user_prompt(
            input_data=input_data,
            video_title="测试",
            author="作者",
            description="描述"
        )

        self.assertIn("对话数量约束", old_style_prompt)
        self.assertIn("2 个对话", old_style_prompt)
        self.assertIn("待校对的JSON数据", old_style_prompt)

        # 测试新版调用方式
        dialogs_text = "[spk0]: 你好\n[spk1]: 你好"
        key_info = "- 人名: 张三, 李四"

        new_style_prompt = build_structured_calibrate_user_prompt(
            dialogs_text=dialogs_text,
            video_title="测试",
            description="描述",
            key_info=key_info,
            dialog_count=2,
            min_ratio=0.95,
        )

        self.assertIn("长度要求", new_style_prompt)
        self.assertIn("对话数量约束", new_style_prompt)
        self.assertIn("2 个对话", new_style_prompt)
        self.assertIn("关键信息", new_style_prompt)
        self.assertIn("待校对的对话文本", new_style_prompt)


if __name__ == "__main__":
    unittest.main()
