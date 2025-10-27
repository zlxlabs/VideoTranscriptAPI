import requests
import time
from typing import Optional
from loguru import logger

def call_llm_api(model: str, prompt: str, api_key: str, base_url: str,
                 max_retries: int = 2, retry_delay: int = 5,
                 reasoning_effort: Optional[str] = None,
                 task_type: str = "unknown") -> str:
    """
    调用大语言模型API，支持自动重试机制

    Args:
        model: 模型名称
        prompt: 提示词
        api_key: API密钥
        base_url: API基础URL
        max_retries: 最大重试次数，默认2次
        retry_delay: 重试间隔秒数，默认5秒
        reasoning_effort: 推理强度级别 ("none", "low", "medium", "high")，默认None
        task_type: 任务类型，用于日志追踪，默认"unknown"
                   可选值: calibrate, summary, validate, speaker_inference, etc.

    Returns:
        str: 模型返回的内容
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }

    # 如果指定了 reasoning_effort，则添加到请求中
    if reasoning_effort is not None:
        data["reasoning_effort"] = reasoning_effort

    # 计算基本统计信息
    prompt_length = len(prompt)

    last_error = None
    start_time = time.time()

    # 总共尝试 max_retries + 1 次（初始尝试 + 重试次数）
    for attempt in range(max_retries + 1):
        try:
            # 格式化 reasoning_effort 显示：None → disabled, 其他 → 原值
            reasoning_status = 'disabled' if reasoning_effort is None else reasoning_effort

            # INFO 级别：记录任务类型、模型、推理级别
            logger.info(
                f"[{task_type.upper()}] Model: {model} | Reasoning: {reasoning_status} | "
                f"Attempt {attempt + 1}/{max_retries + 1}"
            )
            # DEBUG 级别：记录详细参数
            logger.debug(f"[{task_type.upper()}] Prompt length: {prompt_length} chars")

            resp = requests.post(base_url, json=data, headers=headers, timeout=180)
            resp.raise_for_status()
            result = resp.json()

            # 成功获取结果
            content = result["choices"][0]["message"]["content"].strip()
            duration = time.time() - start_time

            # INFO 级别：记录成功
            if attempt > 0:
                logger.info(f"[{task_type.upper()}] ✓ Succeeded after {attempt + 1} attempts")
            else:
                logger.info(f"[{task_type.upper()}] ✓ Succeeded")

            # DEBUG 级别：记录响应详情
            logger.debug(
                f"[{task_type.upper()}] Response: {len(content)} chars | Duration: {duration:.2f}s"
            )

            return content

        except requests.exceptions.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response else "unknown"
            error_msg = f"HTTP {status_code}: {str(e)}"
            logger.warning(
                f"[{task_type.upper()}] ✗ {error_msg} | Attempt {attempt + 1}/{max_retries + 1}"
            )

        except requests.exceptions.RequestException as e:
            last_error = e
            error_msg = f"Network error: {str(e)}"
            logger.warning(
                f"[{task_type.upper()}] ✗ {error_msg} | Attempt {attempt + 1}/{max_retries + 1}"
            )

        except (KeyError, ValueError) as e:
            last_error = e
            error_msg = f"Parse error: {str(e)}"
            logger.warning(
                f"[{task_type.upper()}] ✗ {error_msg} | Attempt {attempt + 1}/{max_retries + 1}"
            )

        except Exception as e:
            last_error = e
            error_msg = f"Unknown error: {str(e)}"
            logger.warning(
                f"[{task_type.upper()}] ✗ {error_msg} | Attempt {attempt + 1}/{max_retries + 1}"
            )

        # 如果不是最后一次尝试，则等待后重试
        if attempt < max_retries:
            logger.debug(f"[{task_type.upper()}] Waiting {retry_delay}s before retry...")
            time.sleep(retry_delay)

    # 所有尝试都失败了
    total_duration = time.time() - start_time
    logger.error(
        f"[{task_type.upper()}] ✗ Failed after {max_retries + 1} attempts | "
        f"Total duration: {total_duration:.2f}s | Last error: {last_error}"
    )
    return f"【LLM call failed】{last_error}" 