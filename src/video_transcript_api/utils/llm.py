import requests
import logging
import time
from typing import Optional

def call_llm_api(model: str, prompt: str, api_key: str, base_url: str, 
                 max_retries: int = 2, retry_delay: int = 5) -> str:
    """
    调用大语言模型API，支持自动重试机制
    
    Args:
        model: 模型名称
        prompt: 提示词
        api_key: API密钥
        base_url: API基础URL
        max_retries: 最大重试次数，默认2次
        retry_delay: 重试间隔秒数，默认5秒
        
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
    
    last_error = None
    
    # 总共尝试 max_retries + 1 次（初始尝试 + 重试次数）
    for attempt in range(max_retries + 1):
        try:
            logging.info(f"LLM API 调用尝试 {attempt + 1}/{max_retries + 1}")
            
            resp = requests.post(base_url, json=data, headers=headers, timeout=180)
            resp.raise_for_status()
            result = resp.json()
            
            # 成功获取结果
            content = result["choices"][0]["message"]["content"].strip()
            if attempt > 0:
                logging.info(f"LLM API 调用在第 {attempt + 1} 次尝试后成功")
            return content
            
        except requests.exceptions.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response else "未知"
            error_msg = f"HTTP {status_code} 错误: {str(e)}"
            logging.warning(f"LLM API 调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_msg}")
            
        except requests.exceptions.RequestException as e:
            last_error = e
            error_msg = f"网络请求错误: {str(e)}"
            logging.warning(f"LLM API 调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_msg}")
            
        except (KeyError, ValueError) as e:
            last_error = e
            error_msg = f"响应解析错误: {str(e)}"
            logging.warning(f"LLM API 调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_msg}")
            
        except Exception as e:
            last_error = e
            error_msg = f"未知错误: {str(e)}"
            logging.warning(f"LLM API 调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {error_msg}")
        
        # 如果不是最后一次尝试，则等待后重试
        if attempt < max_retries:
            logging.info(f"等待 {retry_delay} 秒后进行重试...")
            time.sleep(retry_delay)
    
    # 所有尝试都失败了
    logging.error(f"LLM API 调用在 {max_retries + 1} 次尝试后仍然失败")
    return f"【大模型调用失败】{last_error}" 