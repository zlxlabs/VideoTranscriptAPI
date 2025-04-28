import os
import sys
import json
import pytest
import unittest
from unittest.mock import MagicMock, patch

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from transcriber import Transcriber, SRTConverter


class TestSRTConverter(unittest.TestCase):
    """测试SRT转换器"""
    
    def setUp(self):
        """创建临时SRT文件用于测试"""
        self.test_srt_content = """1
00:00:00,000 --> 00:00:05,000
这是第一行字幕

2
00:00:05,500 --> 00:00:10,000
这是第二行字幕

3
00:00:10,500 --> 00:00:15,000
这是第三行字幕
"""
        self.test_srt_file = "test_subtitle.srt"
        with open(self.test_srt_file, "w", encoding="utf-8") as f:
            f.write(self.test_srt_content)
    
    def tearDown(self):
        """清理测试文件"""
        if os.path.exists(self.test_srt_file):
            os.remove(self.test_srt_file)
    
    def test_parse_srt(self):
        """测试解析SRT文件"""
        converter = SRTConverter(self.test_srt_file)
        self.assertEqual(len(converter.segments), 3)
        self.assertEqual(converter.segments[0]['index'], 1)
        self.assertEqual(converter.segments[0]['text'], "这是第一行字幕")
        self.assertEqual(converter.segments[0]['start_time'], "00:00:00,000")
        self.assertEqual(converter.segments[0]['end_time'], "00:00:05,000")
    
    def test_to_lrc(self):
        """测试转换为LRC格式"""
        converter = SRTConverter(self.test_srt_file)
        lrc_content = converter.to_lrc()
        
        # 验证是否包含LRC文件头
        self.assertIn("[ti:Transcription]", lrc_content)
        self.assertIn("[ar:Whisper]", lrc_content)
        
        # 验证是否包含时间戳和文本
        self.assertIn("[00:00:00]这是第一行字幕", lrc_content)
        self.assertIn("[00:00:05]这是第二行字幕", lrc_content)
        self.assertIn("[00:00:10]这是第三行字幕", lrc_content)
    
    def test_to_text(self):
        """测试转换为纯文本"""
        converter = SRTConverter(self.test_srt_file)
        text_content = converter.to_text()
        
        # 验证纯文本内容
        expected_text = "这是第一行字幕\n这是第二行字幕\n这是第三行字幕"
        self.assertEqual(text_content, expected_text)


@patch('transcriber.transcriber.WhisperModel')
class TestTranscriber(unittest.TestCase):
    """测试转录器"""
    
    def setUp(self):
        """设置测试环境"""
        # 创建临时配置
        self.test_config = {
            "transcription": {
                "model_path": "./models/test-model",
                "device": "cpu",
                "compute_type": "int8",
                "language": "zh"
            },
            "storage": {
                "output_dir": "./test_output"
            }
        }
        
        # 创建临时输出目录
        os.makedirs("./test_output", exist_ok=True)
    
    def tearDown(self):
        """清理测试环境"""
        # 删除测试输出目录中的文件
        for file in os.listdir("./test_output"):
            os.remove(os.path.join("./test_output", file))
        
        # 删除测试输出目录
        if os.path.exists("./test_output"):
            os.rmdir("./test_output")
    
    def test_transcribe(self, mock_whisper_model):
        """测试转录功能"""
        # 创建模拟WhisperModel和转录结果
        mock_model_instance = mock_whisper_model.return_value
        
        # 模拟segments生成器返回的内容
        mock_segment1 = MagicMock()
        mock_segment1.start = 0.0
        mock_segment1.end = 5.0
        mock_segment1.text = "这是第一段语音"
        
        mock_segment2 = MagicMock()
        mock_segment2.start = 5.5
        mock_segment2.end = 10.0
        mock_segment2.text = "这是第二段语音"
        
        # 模拟info对象
        mock_info = MagicMock()
        mock_info.language = "zh"
        mock_info.language_probability = 0.99
        
        # 设置transcribe方法的返回值
        mock_model_instance.transcribe.return_value = ([mock_segment1, mock_segment2], mock_info)
        
        # 创建转录器实例
        transcriber = Transcriber(config=self.test_config)
        
        # 模拟SRTConverter类
        with patch('transcriber.transcriber.SRTConverter') as mock_converter:
            mock_converter_instance = mock_converter.return_value
            mock_converter_instance.to_lrc.return_value = "[00:00:00]这是第一段语音\n[00:00:05]这是第二段语音"
            
            # 调用转录方法
            result = transcriber.transcribe("test_audio.mp3", "test_output")
            
            # 验证结果
            self.assertIn("srt_path", result)
            self.assertIn("lrc_path", result)
            self.assertIn("json_path", result)
            self.assertIn("transcript", result)
            self.assertEqual(result["transcript"], "这是第一段语音 这是第二段语音")
            
            # 验证SRT文件内容是否写入
            srt_path = result["srt_path"]
            self.assertTrue(os.path.exists(srt_path))
            
            # 验证JSON文件内容是否写入
            json_path = result["json_path"]
            self.assertTrue(os.path.exists(json_path))
            
            # 验证模型是否正确调用
            mock_model_instance.transcribe.assert_called_once()


if __name__ == '__main__':
    unittest.main() 