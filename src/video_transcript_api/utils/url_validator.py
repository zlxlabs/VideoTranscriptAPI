"""
URL 安全验证模块

提供 URL 安全性验证功能，防止 SSRF（Server-Side Request Forgery）攻击。
验证 URL 的协议、主机名和 IP 地址是否安全。
"""

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlparse
from typing import Optional

from .logging import setup_logger, load_config

logger = setup_logger("url_validator")


class URLValidationError(ValueError):
    """URL 验证失败的异常"""
    pass


@lru_cache(maxsize=1)
def _load_allowlist() -> tuple:
    """
    加载内网下载源白名单（IP 或 CIDR 网段）。

    来源：config.jsonc 的 security.download_url_allowlist 字段，
    用于放行明确可信的内网地址（如局域网录制服务器），同时保持对其他
    私有/保留地址的 SSRF 防护。

    Returns:
        tuple: 解析后的 ip_network 对象元组；配置缺失或非法时返回空元组
    """
    try:
        config = load_config()
        entries = (config.get("security", {}) or {}).get("download_url_allowlist", []) or []
    except Exception as e:
        logger.warning(f"Failed to load download_url_allowlist: {e}")
        return tuple()

    networks = []
    for entry in entries:
        try:
            # strict=False 允许传入单个 IP（自动按 /32、/128 处理）
            networks.append(ipaddress.ip_network(str(entry), strict=False))
        except ValueError as e:
            logger.warning(f"Invalid download_url_allowlist entry ignored: {entry} ({e})")
    if networks:
        logger.info(f"Loaded {len(networks)} download_url_allowlist entries")
    return tuple(networks)


def _is_allowlisted(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """检查 IP 是否在内网下载源白名单内（已显式信任，可放行）。"""
    for network in _load_allowlist():
        if ip.version == network.version and ip in network:
            return True
    return False


def validate_url_safe(url: str) -> str:
    """
    验证 URL 是否安全（防止 SSRF 攻击）

    检查项目：
    1. 仅允许 http/https 协议
    2. 禁止访问私有 IP 地址（RFC 1918、环回地址、链路本地等）
    3. 禁止访问云元数据端点（169.254.169.254）
    4. DNS 解析后再次验证 IP 安全性（防止 DNS rebinding）

    Args:
        url: 要验证的 URL

    Returns:
        str: 验证通过的 URL（原样返回）

    Raises:
        URLValidationError: URL 不安全时抛出
    """
    _validate_and_resolve(url)
    return url.strip()


def validate_url_safe_with_ip(url: str) -> tuple:
    """
    与 validate_url_safe 等价的安全校验，额外返回本次校验实际解析并验证过的
    IP 地址。

    背景：validate_url_safe 只做"校验通过/不通过"的判断，校验时解析到的 IP
    并不会返回给调用方；如果调用方随后再让 requests/urllib3 对同一个域名
    重新发起一次独立的 DNS 解析，攻击者控制的域名完全可能在两次解析之间
    切换 DNS 记录（DNS rebinding），从而让"已校验通过"和"实际连接"的目标
    不是同一个地址，SSRF 防护出现 TOCTOU 窗口。

    调用方应该用本函数返回的 IP 直接建立连接（"钉住"该 IP），而不是把
    hostname 交给网络库去重新解析，才能真正堵住这个窗口。

    Args:
        url: 要验证的 URL

    Returns:
        tuple[str, str | None]: (校验通过的 URL, 已验证的 IP)
            IP 为 None 的两种情况：
            1. hostname 已经是字面量 IP —— 无需额外解析，调用方直接用
               hostname 本身连接即可，不存在两次解析不一致的风险
            2. DNS 解析失败（gaierror）—— validate_url_safe 对此采用"放行，
               可能是瞬时故障"的宽松策略，此时没有已验证的 IP 可钉，调用方
               应退化为不钉 IP 的普通请求（等价于本次修复之前的行为，不会
               引入新的失败模式）

    Raises:
        URLValidationError: URL 不安全时抛出
    """
    url = url.strip() if isinstance(url, str) else url
    hostname, ip = _validate_and_resolve(url)
    if ip is None:
        try:
            # hostname 本身就是字面量 IP 时，_check_resolved_ip 的 DNS 解析是
            # 空操作（getaddrinfo 对字面量 IP 直接原样返回，不做网络查询），
            # 此时直接把 hostname 当作已验证 IP 返回，语义上等价且更直观。
            ip = str(ipaddress.ip_address(hostname))
        except ValueError:
            ip = None
    return url, ip


def _validate_and_resolve(url: str) -> tuple:
    """
    validate_url_safe / validate_url_safe_with_ip 共用的校验实现。

    Args:
        url: 要验证的 URL

    Returns:
        tuple[str, str | None]: (hostname, 校验时选定的已验证 IP 或 None)

    Raises:
        URLValidationError: URL 不安全时抛出
    """
    if not url or not isinstance(url, str):
        raise URLValidationError("URL must be a non-empty string")

    url = url.strip()

    # 1. 检查协议（仅允许 http/https）
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise URLValidationError(
            f"Unsupported URL scheme: '{parsed.scheme}'. Only http/https allowed"
        )

    # 2. 检查主机名
    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("URL has no hostname")

    # 3. 检查是否为已知的危险主机名
    _check_dangerous_hostname(hostname)

    # 4. DNS 解析并检查解析后的 IP
    resolved_ip = _check_resolved_ip(hostname)

    logger.debug(f"URL safety check passed: {url[:100]}")
    return hostname, resolved_ip


def _check_dangerous_hostname(hostname: str) -> None:
    """
    检查主机名是否指向危险地址

    Args:
        hostname: 主机名

    Raises:
        URLValidationError: 主机名不安全时抛出
    """
    hostname_lower = hostname.lower()

    # 云元数据端点域名
    dangerous_hostnames = {
        "metadata.google.internal",
        "metadata.google",
        "169.254.169.254",
    }
    if hostname_lower in dangerous_hostnames:
        raise URLValidationError(
            f"Access to cloud metadata endpoint is blocked: {hostname}"
        )

    # 禁止 localhost 及相关变体
    if hostname_lower in ("localhost", "0.0.0.0", "[::]", "[::1]"):
        raise URLValidationError(
            f"Access to localhost is blocked: {hostname}"
        )

    # 尝试直接解析为 IP 地址进行检查
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # 不是 IP 地址格式（是域名），后续通过 DNS 检查
        return

    if _is_private_ip(ip) and not _is_allowlisted(ip):
        raise URLValidationError(
            f"Access to private/reserved IP is blocked: {hostname}"
        )


def _check_resolved_ip(hostname: str) -> Optional[str]:
    """
    DNS 解析主机名并验证解析结果是否安全

    防止通过 DNS rebinding 或指向内网 IP 的域名绕过检查。

    Args:
        hostname: 要解析的主机名

    Returns:
        str | None: 本次解析中第一个通过安全校验的 IP（供调用方"钉住"该 IP
                     发起后续真正的网络连接，消除校验与连接之间独立重新解析
                     造成的 TOCTOU 窗口）；DNS 解析失败时返回 None（沿用既有
                     的宽松放行策略）

    Raises:
        URLValidationError: 解析到的 IP 不安全时抛出
    """
    try:
        # 获取所有解析结果
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        if not addr_infos:
            raise URLValidationError(f"DNS resolution failed: {hostname}")

        resolved_ip: Optional[str] = None
        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_private_ip(ip) and not _is_allowlisted(ip):
                raise URLValidationError(
                    f"DNS resolved to private/reserved IP: {hostname} -> {ip_str}"
                )
            if resolved_ip is None:
                # 只钉住第一条解析结果，和不做钉定时 socket 连接默认尝试的
                # 地址顺序保持一致，避免钉定行为悄悄改变正常连接目标
                resolved_ip = ip_str

        return resolved_ip

    except socket.gaierror as e:
        # DNS 解析失败 — 允许继续（可能是临时 DNS 问题）
        logger.warning(f"DNS resolution failed for {hostname}: {e}")
        return None
    except URLValidationError:
        raise
    except Exception as e:
        logger.warning(f"Unexpected error during DNS check for {hostname}: {e}")
        return None


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    检查 IP 地址是否为私有/保留地址

    覆盖范围：
    - RFC 1918 私有地址（10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16）
    - 环回地址（127.0.0.0/8, ::1）
    - 链路本地地址（169.254.0.0/16, fe80::/10）
    - 多播地址
    - 保留地址

    Args:
        ip: IP 地址对象

    Returns:
        bool: True 表示是私有/保留地址，不安全
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
