import os
import sys
import json
import pytest
import unittest
from unittest.mock import MagicMock, patch

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from transcriber import Transcriber


@patch('transcriber.transcriber.client_transcriber')
class TestTranscriber(unittest.TestCase):
    """测试转录器"""
    
    def setUp(self):
        """设置测试环境"""
        # 创建临时配置
        self.test_config = {
            "capswriter": {
                "server_url": "ws://localhost:6006"
            },
            "storage": {
                "output_dir": "./test_output"
            }
        }
        
        # 创建临时输出目录
        os.makedirs("./test_output", exist_ok=True)
        
        # 创建测试文件
        self.test_merge_txt_content = "这是测试的转录文本，包含一些句子。这是第二句。"
        self.test_audio_file = "test_audio.mp3"
        self.test_merge_txt = "test_audio.merge.txt"
        
        # 写入merge.txt测试文件
        with open(self.test_merge_txt, "w", encoding="utf-8") as f:
            f.write(self.test_merge_txt_content)
        
        # 创建空的测试音频文件
        with open(self.test_audio_file, "w", encoding="utf-8") as f:
            f.write("模拟音频文件")
    
    def tearDown(self):
        """清理测试环境"""
        # 删除测试文件
        for file in [self.test_audio_file, self.test_merge_txt]:
            if os.path.exists(file):
                os.remove(file)
        
        # 删除测试输出目录中的文件
        for file in os.listdir("./test_output"):
            os.remove(os.path.join("./test_output", file))
        
        # 删除测试输出目录
        if os.path.exists("./test_output"):
            os.rmdir("./test_output")
    
    def test_transcribe(self, mock_client_transcriber):
        """测试转录功能"""
        # 设置模拟客户端的返回值
        mock_client_transcriber.transcribe.return_value = (True, [self.test_merge_txt])
        
        # 创建转录器实例
        transcriber = Transcriber(config=self.test_config)
        
        # 调用转录方法
        result = transcriber.transcribe(self.test_audio_file, "test_output")
        
        # 验证客户端转录方法被调用
        mock_client_transcriber.transcribe.assert_called_once_with(self.test_audio_file)
        
        # 验证结果
        self.assertIn("transcript", result)
        self.assertEqual(result["transcript"], self.test_merge_txt_content)
        
        # 验证文件路径存在于结果中
        self.assertIn("merge_txt_path", result)
        
        # 验证转录结果是否包含merge.txt内容
        self.assertEqual(result["transcript"], self.test_merge_txt_content)
    
    def test_transcribe_error(self, mock_client_transcriber):
        """测试转录失败的情况"""
        # 设置模拟客户端的返回值为失败
        mock_client_transcriber.transcribe.return_value = (False, [])
        
        # 创建转录器实例
        transcriber = Transcriber(config=self.test_config)
        
        # 调用转录方法应该抛出异常
        with self.assertRaises(RuntimeError):
            transcriber.transcribe(self.test_audio_file, "test_output")
        
        # 验证客户端转录方法被调用
        mock_client_transcriber.transcribe.assert_called_once_with(self.test_audio_file)


if __name__ == '__main__':
    unittest.main() 