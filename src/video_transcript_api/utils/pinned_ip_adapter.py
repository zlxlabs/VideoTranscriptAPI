"""
IP 钉定（pinned）HTTP(S) 传输适配器 —— 用于闭合 SSRF 校验的 DNS rebinding
TOCTOU 窗口。

背景：url_validator.validate_url_safe 先自行解析域名并校验解析到的 IP 是否
安全，但如果调用方随后直接把原始 URL（域名形式）交给 requests/urllib3 去
发起真正的网络请求，requests 会*重新独立解析一次 DNS*。攻击者控制的域名
完全可以在这两次解析之间切换 DNS 记录（先解析成公网 IP 通过校验，实际连接
时再解析成内网/云元数据 IP），从而绕过 SSRF 防护 —— 这是经典的 DNS
rebinding / Time-Of-Check-Time-Of-Use（TOCTOU）漏洞。

本模块提供的 PinnedIPHTTPAdapter 把"校验"和"连接"绑定到同一次 DNS 解析
结果上：调用方先用 url_validator 拿到已验证的 IP，再用这个 IP 构造适配器，
后续无论 requests 内部怎么处理，实际 TCP 连接都会直接打到这个 IP，不会再
触发任何新的 DNS 查询。

同时必须保证这么做不会削弱 HTTPS 的安全性：
- SNI（server_hostname）仍然发送真实域名，保证走 SNI 路由的 CDN/负载均衡
  能正确识别目标站点；
- 证书主机名校验（assert_hostname）仍然按真实域名匹配证书，而不是按 IP
  匹配 —— 否则大多数证书（CN/SAN 都是域名而非 IP）校验必然失败，为了"图
  省事"而把 assert_hostname 也设成 IP 或直接关闭校验，等于用一个安全问题
  换另一个安全问题。

实现参考了社区里 forced-ip-https-adapter 这类小工具的通用做法（重写请求
URL 的 host 部分为目标 IP、保留原始 Host 头、通过 urllib3
HTTPSConnectionPool 的 assert_hostname/server_hostname 钉住证书校验和 SNI
用的主机名），但不引入新依赖，直接基于项目已有的 requests/urllib3 自实现。
"""

from urllib.parse import urlparse, urlunparse

from requests.adapters import HTTPAdapter

from .logging import setup_logger

logger = setup_logger("pinned_ip_adapter")


class PinnedIPHTTPAdapter(HTTPAdapter):
    """
    requests HTTPAdapter 子类：把对 `hostname` 的连接钉定到 `pinned_ip`。

    一个实例只服务于一个 (hostname, pinned_ip) 目标对，用完即弃 —— 调用方
    应该为每一次 SSRF 校验（含每一跳重定向）都构造一个新实例，不要跨不同
    目标复用，否则 send() 会因为主机名不匹配而拒绝请求（防御性检查，避免
    悄悄把请求发到一个从未被校验过的目标上）。

    工作原理：
    1. send() 把请求 URL 中的 hostname 替换成 pinned_ip，这样 requests/
       urllib3 内部的连接池查找、真正的 socket.connect() 都直接使用这个 IP，
       不会再触发针对 hostname 的新 DNS 解析。
    2. 显式设置 Host 头为原始 hostname —— 否则 http.client 会根据连接池的
       host（此时是 IP）自动生成 Host 头，破坏虚拟主机路由。
    3. HTTPS 场景下，init_poolmanager() 把 server_hostname（SNI）和
       assert_hostname（证书主机名校验目标）都固定为原始 hostname，让
       urllib3 在"物理连接 IP"的同时，仍然按真实域名做 SNI 和证书校验。
       这条只覆盖直连场景使用的 PoolManager。
    4. 代理场景（HTTP(S)_PROXY 配置存在时）走的是另一套连接池
       ——urllib3.ProxyManager，requests.adapters.HTTPAdapter 通过
       proxy_manager_for() 创建/缓存它，不会自动继承 init_poolmanager()
       给 PoolManager 注入的参数。proxy_manager_for() 在本类中被覆写，
       与 init_poolmanager() 做对称处理：同样按 is_https 注入
       server_hostname/assert_hostname，确保 HTTPS 代理隧道
       （CONNECT）内部实际连接目标的 HTTPSConnectionPool 也钉住真实
       域名做 SNI/证书校验，同时 send() 已把请求 URL 钉成 pinned_ip，
       所以代理收到的 CONNECT 目标本身就是已校验 IP，钉定语义与直连
       路径完全一致。
    """

    def __init__(self, hostname: str, pinned_ip: str, is_https: bool, **kwargs):
        """
        参数:
            hostname: 校验时使用、且后续 TLS 校验/Host 头仍要使用的真实域名
            pinned_ip: url_validator 校验通过时实际解析并检查过的 IP，
                       真正的 TCP 连接将钉定到这个地址
            is_https: 目标是否为 https —— 只有 https 才需要注入
                       server_hostname/assert_hostname（这两个是 urllib3
                       HTTPSConnectionPool 专属的 TLS 参数，普通 HTTP 连接
                       池不认识，也不需要）
        """
        self._hostname = hostname
        self._pinned_ip = pinned_ip
        self._is_https = is_https
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        """
        初始化底层 urllib3 PoolManager。

        HTTPS 场景下注入 server_hostname / assert_hostname，确保连接池虽然
        以 pinned_ip 为 key，SNI 和证书主机名校验依然针对真实域名进行
        （否则默认会退化为用连接池的 host，即 IP 本身，绝大多数证书的
        CN/SAN 都是域名而非 IP，会导致校验失败或被迫关闭校验）。
        """
        if self._is_https:
            kwargs["server_hostname"] = self._hostname
            kwargs["assert_hostname"] = self._hostname
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        """
        返回用于经由 `proxy` 连接目标的 urllib3 ProxyManager（HTTPS 场景下
        走 CONNECT 隧道），与 init_poolmanager() 对称的另一半 TLS 参数注入
        （ci-gate review 第四轮修复）。

        背景：requests 对"直连"和"走代理"使用两套完全独立的连接池实现
        ——PoolManager（本类 init_poolmanager() 已经把 server_hostname/
        assert_hostname 写进它的 connection_pool_kw）和 ProxyManager。
        HTTPAdapter.get_connection_with_tls_context() 调用本方法时不会
        传入任何 TLS 相关参数（当前 requests 版本的调用是
        `self.proxy_manager_for(proxy)`，不带 kwargs），所以 ProxyManager
        默认拿不到这两个参数，必须在这里主动补上。

        传递链路（已对照本项目锁定的 requests/urllib3 版本源码逐层验证）：
        本方法把 server_hostname/assert_hostname 塞进 proxy_kwargs 后交给
        super().proxy_manager_for()，requests 的实现会把这些 kwargs 原样
        透传给 urllib3.proxy_from_url() -> ProxyManager.__init__()。
        ProxyManager.__init__ 只认领它自己命名的几个参数（proxy_headers/
        proxy_ssl_context/use_forwarding_for_https/proxy_assert_hostname/
        proxy_assert_fingerprint），其余（含 server_hostname/
        assert_hostname）作为 **connection_pool_kw 转交给父类
        PoolManager.__init__，存成 self.connection_pool_kw。当请求 scheme
        为 https 时，ProxyManager.connection_from_host() 会把这次调用委托
        给 PoolManager.connection_from_host()，后者用
        `_merge_pool_kwargs()` 把 self.connection_pool_kw 并入新建连接池
        （即 CONNECT 隧道内部实际连接目标用的 HTTPSConnectionPool）的构造
        参数——这条链路和直连场景下 init_poolmanager() 注入
        PoolManager.connection_pool_kw 的机制完全对称，唯一区别是多绕了
        ProxyManager 这一层。

        HTTP（非 HTTPS）场景不注入：没有 TLS 握手，ProxyManager 会把请求
        直接转发给代理（request.url 已被 send() 钉成 pinned IP 的绝对
        形式），不涉及 SNI/证书主机名校验，注入这两个 TLS 专属 kwarg 对
        非 TLS 转发没有意义（对齐 init_poolmanager() 现有的同款 is_https
        条件判断，避免无意义的参数污染代理连接池的构造参数）。
        """
        if self._is_https:
            proxy_kwargs.setdefault("server_hostname", self._hostname)
            proxy_kwargs.setdefault("assert_hostname", self._hostname)
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def send(self, request, **kwargs):
        """
        发送请求前，把请求 URL 的主机部分重写为已钉定的 IP，并强制设置
        Host 头为原始域名（按原始 URL 的端口决定是否显式带端口），随后
        交给父类完成实际发送。
        """
        original_url = request.url
        request.url = self._pin_to_ip(original_url)
        request.headers["Host"] = self._build_host_header(original_url)
        return super().send(request, **kwargs)

    def _build_host_header(self, url: str) -> str:
        """
        按 RFC 7230/3986 构造 Host 请求头：
        - 非默认端口（http 非 80 / https 非 443）显式带上 `host:port`；
          默认端口省略端口（原实现固定写裸 hostname，非默认端口的请求会
          丢端口，按 Host+端口路由的源站/反代可能因此错路由或直接拒绝）。
        - hostname 为 IPv6 字面量时用方括号包裹（`[addr]` / `[addr]:port`）
          ——self._hostname 来自 urlparse().hostname，IPv6 场景下已经是去
          掉方括号的裸地址（如 "2001:db8::1"），这里按 RFC 3986 补回来。

        用 urllib.parse 解析端口而不是手写字符串拼接，避免遗漏 IPv6 或
        端口边界情况。

        参数:
            url: 原始请求 URL（重写为钉定 IP 之前的那个，端口信息取自此处）
        """
        port = urlparse(url).port
        default_port = 443 if self._is_https else 80

        host = f"[{self._hostname}]" if ":" in self._hostname else self._hostname
        if port is None or port == default_port:
            return host
        return f"{host}:{port}"

    def _pin_to_ip(self, url: str) -> str:
        """
        把 URL 的 hostname 部分替换成已钉定的 IP，保留 scheme/port/path 等
        其余部分不变。

        抛出:
            ValueError: URL 的 hostname 与本实例钉定的 hostname 不一致 ——
                        说明调用方复用了该实例服务于另一个未经校验的目标，
                        这是编程错误，必须拒绝而不是静默连接到未知主机
        """
        parsed = urlparse(url)
        if parsed.hostname != self._hostname:
            raise ValueError(
                f"PinnedIPHTTPAdapter 已钉定 {self._hostname}，"
                f"但收到了针对 {parsed.hostname} 的请求，拒绝发送"
            )

        host_part = f"[{self._pinned_ip}]" if ":" in self._pinned_ip else self._pinned_ip
        netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
        return urlunparse(parsed._replace(netloc=netloc))
