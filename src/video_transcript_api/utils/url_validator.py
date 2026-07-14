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

    只返回单条 IP，是历史接口，保留给已有调用方（如
    tests/integration/test_failure_status_persistence.py 的桩）使用；新的
    调用方如需要"首选地址不可达时换下一个"的钉定重试能力，请使用
    validate_url_safe_with_ips（codex-review R8 #2）——本函数内部就是取
    该函数候选列表的第一条，语义完全等价，不是两套独立实现。

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
    url, ips = validate_url_safe_with_ips(url, max_candidates=1)
    return url, (ips[0] if ips else None)


def validate_url_safe_with_ips(url: str, max_candidates: int = 3) -> tuple:
    """
    与 validate_url_safe 等价的安全校验，额外返回本次校验实际解析并验证过的
    全部候选 IP（按 DNS 返回顺序，最多 max_candidates 个）。

    背景（codex-review R8 #2）：validate_url_safe_with_ip 只钉住 DNS 解析
    结果里的第一条地址。双栈/多节点域名很常见——例如同时有 AAAA 和 A
    记录，或同一域名背后挂了多个节点 IP——当第一条地址在当前网络环境下
    恰好不可达（比如 IPv6 不通），"只钉第一条"的实现会反复重试同一个
    死地址，即便同一次解析结果里还有其他可达的候选，也永远不会被尝试到，
    导致本来能下载成功的链接失败。标准 socket 连接（不做钉定时）本来就会
    依次尝试 getaddrinfo 返回的候选地址，这个函数把同样的候选列表暴露给
    调用方，让钉定的下载路径也能做到同等的"首选不可达就换下一个"。

    安全语义不因为暴露多个候选而放松：校验阶段仍然遍历 DNS 解析出的*全部*
    地址做私网/保留地址检查——只要其中任意一条落在私网/保留网段且未被
    download_url_allowlist 放行，整个 hostname 立即判定不安全并抛出异常，
    不会因为凑巧还有别的公网地址而放行（先确认过现状本就是"任一私网地址
    即整体拒绝"，这里原样保留，不引入新的判定分支）。max_candidates 只
    影响返回给调用方的候选数量上限，不影响这条判定的覆盖面。

    Args:
        url: 要验证的 URL
        max_candidates: 最多返回的已验证候选 IP 数（默认 3——取舍：多数
            部署形态下 DNS 解析结果不会超过个位数，前 3 个已经能覆盖常见
            的双栈/多节点场景；同时避免下游钉定重试的候选循环与外层已有
            的下载重试循环相乘，堆出过多请求）

    Returns:
        tuple[str, list[str]]: (校验通过的 URL, 已验证的候选 IP 列表，
            去重、保持解析顺序，最多 max_candidates 条)
            列表为空表示 DNS 解析失败（gaierror）—— 沿用 validate_url_safe
            "放行，可能是瞬时故障"的宽松策略，此时没有已验证的 IP 可钉，
            调用方应退化为不钉 IP 的普通请求（等价于本次修复之前的行为，
            不会引入新的失败模式）

    Raises:
        URLValidationError: URL 不安全时抛出
    """
    url = url.strip() if isinstance(url, str) else url
    hostname, ips = _validate_and_resolve(url)
    if not ips:
        try:
            # hostname 本身就是字面量 IP 时，_check_resolved_ip 的 DNS 解析是
            # 空操作（getaddrinfo 对字面量 IP 直接原样返回，不做网络查询），
            # 此时直接把 hostname 当作已验证 IP 返回，语义上等价且更直观。
            ips = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            ips = []
    return url, ips[:max_candidates]


def _validate_and_resolve(url: str) -> tuple:
    """
    validate_url_safe / validate_url_safe_with_ip(s) 共用的校验实现。

    Args:
        url: 要验证的 URL

    Returns:
        tuple[str, list[str] | None]: (hostname, 校验时选定的已验证 IP 列表
            或 None)

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
    resolved_ips = _check_resolved_ip(hostname)

    logger.debug(f"URL safety check passed: {url[:100]}")
    return hostname, resolved_ips


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


def _check_resolved_ip(hostname: str) -> Optional[list]:
    """
    DNS 解析主机名并验证解析结果是否安全

    防止通过 DNS rebinding 或指向内网 IP 的域名绕过检查。

    安全判定覆盖全部解析结果（codex-review R8 #2 之前就是如此，这里原样
    保留）：只要任意一条解析到的地址是私网/保留地址且未被
    download_url_allowlist 放行，立即整体拒绝，不会因为凑巧还有别的公网
    地址而放行——不返回"过滤掉私网地址后剩下的公网地址列表"这种更宽松的
    语义。

    Args:
        hostname: 要解析的主机名

    Returns:
        list[str] | None: 本次解析中全部通过安全校验的 IP（按 getaddrinfo
                     返回顺序去重），供调用方按顺序"钉住"逐个尝试连接，
                     首选地址不可达时换下一个（消除校验与连接之间独立
                     重新解析造成的 TOCTOU 窗口，同时不再像早期实现那样
                     只暴露第一条地址）；DNS 解析失败时返回 None（沿用既有
                     的宽松放行策略）

    Raises:
        URLValidationError: 解析到的 IP 不安全时抛出
    """
    try:
        # 获取所有解析结果
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        if not addr_infos:
            raise URLValidationError(f"DNS resolution failed: {hostname}")

        resolved_ips: list = []
        seen_ips: set = set()
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
            if ip_str not in seen_ips:
                # 保持 getaddrinfo 返回顺序去重（同一 IP 可能因不同
                # socktype/protocol 组合在结果里重复出现）
                seen_ips.add(ip_str)
                resolved_ips.append(ip_str)

        return resolved_ips

    except socket.gaierror as e:
        # DNS 解析失败 — 允许继续（可能是临时 DNS 问题）
        logger.warning(f"DNS resolution failed for {hostname}: {e}")
        return None
    except URLValidationError:
        raise
    except Exception as e:
        logger.warning(f"Unexpected error during DNS check for {hostname}: {e}")
        return None


# RFC 6598 运营商级 NAT 共享地址空间（Carrier-Grade NAT / Shared Address
# Space，100.64.0.0/10）。历史遗留问题（codex-review R10）：CPython 的
# ipaddress.IPv4Address.is_private / is_reserved 在这个网段上不生效——
# 实测项目实际运行的 Python（3.11.15 虚拟环境、系统 3.12.3 均如此）：
# ip_address("100.64.0.1").is_private == False、.is_reserved == False，
# 甚至 .is_global 也是 False（即 Python 自己认为它不可公网路由，却没有
# 归进 is_private/is_reserved 这两个既有判定里）。不能依赖某个 Python
# 版本才有的 is_private 行为，必须显式判断，避免同类"版本敏感"的判定
# 缺口。该网段常被云厂商 NAT 网关、容器编排网络、运营商内网路由使用，
# 落在 SSRF 防护的私网语义下应当拦截。
#
# 仅 IPv4 有该网段——IPv6 地址空间充裕，RFC 6598 没有对应的 IPv6"共享
# 地址空间"，不存在同类判定缺口，因此不需要为 IPv6 追加规则。
_CGNAT_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    检查 IP 地址是否为私有/保留地址

    覆盖范围：
    - RFC 1918 私有地址（10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16）
    - RFC 6598 运营商级 NAT 共享地址空间（100.64.0.0/10）——显式判断，
      不依赖 is_private/is_reserved（见上方 _CGNAT_IPV4_NETWORK 注释）
    - 环回地址（127.0.0.0/8, ::1）
    - 链路本地地址（169.254.0.0/16, fe80::/10）
    - 多播地址
    - 保留地址

    Args:
        ip: IP 地址对象

    Returns:
        bool: True 表示是私有/保留地址，不安全
    """
    if ip.version == 4 and ip in _CGNAT_IPV4_NETWORK:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
