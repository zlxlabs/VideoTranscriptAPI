#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import argparse
import json

# 添加src目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def _llm_effective_values(config: dict) -> dict:
    """报告 9 个白名单 LLM 参数的生效值与来源（--check-config 成功诊断）。

    生效值一律取自真实解析器 LLMConfig.from_dict——与 --start 走同一个解析器，
    这里不维护第二套手算默认值；本函数只补 from_dict 不携带的「来源」信息。

    来源标签复刻 from_dict 的真实分支，不按「值好不好看」编造：
      - 前 8 项在 from_dict 里都是裸 dict.get(key, default)，键存在（哪怕
        显式 null）就不会回退 default，所以只看键在不在；
      - structured_fallback_strategy 是 9 项里唯一走真值派生的
        （`if not structured_fallback_strategy:`），空串/None/缺键都命中
        派生分支，此时 source 为 derived。

    白名单不含任何凭据（api_key/base_url/webhook/token 都不在内），因此这行
    JSON 可以安全打印。唯一消费者就是 --check-config 这一个 CLI 分支。
    """
    from video_transcript_api.llm.core.config import LLMConfig

    llm_section = config.get("llm") or {}
    parsed = LLMConfig.from_dict(config)
    segmentation = llm_section.get("segmentation") or {}
    calibration = llm_section.get("structured_calibration") or {}

    plain_get_fields = (
        # llm.segmentation.*
        ("enable_threshold", segmentation),
        ("segment_size", segmentation),
        ("max_segment_size", segmentation),
        # llm.structured_calibration.*
        ("min_chunk_length", calibration),
        ("max_chunk_length", calibration),
        ("preferred_chunk_length", calibration),
        ("calibration_concurrent_limit", calibration),
        # llm.*
        ("structured_calibration_for_plain", llm_section),
    )
    effective = {
        name: {
            "value": getattr(parsed, name),
            "source": "config" if name in section else "default",
        }
        for name, section in plain_get_fields
    }

    quality_validation = calibration.get("quality_validation") or {}
    effective["structured_fallback_strategy"] = {
        "value": parsed.structured_fallback_strategy,
        "source": (
            "config" if quality_validation.get("fallback_strategy") else "derived"
        ),
    }
    return effective


def main():
    """主程序入口函数"""
    parser = argparse.ArgumentParser(description="视频转录API服务")

    # 添加命令行参数
    parser.add_argument("--start", action="store_true", help="启动API服务")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without starting services or creating runtime resources",
    )
    parser.add_argument("--config", help="Configuration file used by --check-config")

    # 解析命令行参数
    args = parser.parse_args()

    if args.check_config:
        from video_transcript_api.api.context import load_and_validate_config

        config = load_and_validate_config(args.config)
        print("Configuration OK")
        # 校验成功之后追加一行机器可解析的生效值；既有 "Configuration OK" 断言不受影响。
        print(
            json.dumps(
                {"llm_effective": _llm_effective_values(config)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif args.start:
        # 启动API服务
        if args.config:
            os.environ["VTAPI_CONFIG"] = args.config
        from video_transcript_api.api.server import start_server

        start_server()
    else:
        # 显示帮助信息
        parser.print_help()

if __name__ == "__main__":
    # 确保工作目录是项目根目录
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main() 
