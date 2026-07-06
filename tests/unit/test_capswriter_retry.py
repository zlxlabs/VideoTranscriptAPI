#!/usr/bin/env python
# coding: utf-8

"""
CapsWriter 客户端重试逻辑测试

覆盖场景:
- 服务端以 close code 1011 (keepalive 超时/内部错误) 主动断连时快速失败, 不再重试
- 其他失败原因仍按 max_retries 正常重试
- _record_close 正确提取关闭帧的 code/reason (含 TCP 异常断开无关闭帧的情况)
"""

import unittest
from unittest.mock import MagicMock, patch

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from video_transcript_api.transcriber.capswriter_client import CapsWriterClient


def _make_client(max_retries=3):
    """构造一个不依赖项目配置文件的客户端实例"""
    with patch.object(CapsWriterClient, "__init__", lambda self: None):
        client = CapsWriterClient()
    client.max_retries = max_retries
    client.retry_delay = 0
    client.websocket = None
    client.current_task_id = None
    client.last_close_code = None
    client.last_close_reason = None
    client.log = MagicMock()
    return client


class TestRecordClose(unittest.TestCase):
    """_record_close 提取关闭帧信息"""

    def test_record_close_with_frame(self):
        client = _make_client()
        exc = ConnectionClosedError(
            rcvd=Close(1011, "keepalive ping timeout"), sent=None
        )
        info = client._record_close(exc)

        self.assertEqual(client.last_close_code, 1011)
        self.assertEqual(client.last_close_reason, "keepalive ping timeout")
        self.assertIn("1011", info)

    def test_record_close_without_frame(self):
        """TCP 层异常断开时没有关闭帧, code/reason 应为 None"""
        client = _make_client()
        exc = ConnectionClosedError(rcvd=None, sent=None)
        info = client._record_close(exc)

        self.assertIsNone(client.last_close_code)
        self.assertIsNone(client.last_close_reason)
        self.assertIn("N/A", info)


class TestTranscribeFileRetry(unittest.TestCase):
    """transcribe_file 的重试与快速失败"""

    def test_fast_fail_on_close_1011(self):
        """服务端 1011 断连时只尝试一次, 不重试"""
        client = _make_client(max_retries=5)
        call_count = {"n": 0}

        async def fake_transcribe(file_path):
            call_count["n"] += 1
            client.last_close_code = 1011
            client.last_close_reason = "keepalive ping timeout"
            return False, []

        client.transcribe_file_async = fake_transcribe
        success, files = client.transcribe_file("dummy.mp4")

        self.assertFalse(success)
        self.assertEqual(files, [])
        self.assertEqual(call_count["n"], 1)

    def test_normal_failure_still_retries(self):
        """普通失败 (无 1011 关闭码) 仍按 max_retries 重试"""
        client = _make_client(max_retries=3)
        call_count = {"n": 0}

        async def fake_transcribe(file_path):
            call_count["n"] += 1
            return False, []

        client.transcribe_file_async = fake_transcribe
        with patch("time.sleep"):
            success, files = client.transcribe_file("dummy.mp4")

        self.assertFalse(success)
        self.assertEqual(call_count["n"], 3)

    def test_success_returns_immediately(self):
        """首次成功直接返回"""
        client = _make_client(max_retries=3)

        async def fake_transcribe(file_path):
            return True, ["dummy.txt"]

        client.transcribe_file_async = fake_transcribe
        success, files = client.transcribe_file("dummy.mp4")

        self.assertTrue(success)
        self.assertEqual(files, ["dummy.txt"])


if __name__ == "__main__":
    unittest.main()
