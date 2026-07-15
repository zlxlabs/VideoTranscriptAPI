import os
import mimetypes
import hashlib
import time
import requests
from urllib.parse import urlparse, urlunparse, unquote, urljoin
from .base import BaseDownloader
from .models import VideoMetadata, DownloadInfo
from ..errors import InvalidURLError
from ..utils.logging import setup_logger
# 模块级导入（而非 from...import 具名导入）：保持 url_validator.validate_url_safe
# 可被测试通过 monkeypatch.setattr("...url_validator.validate_url_safe", ...) 打桩，
# 具名导入会在导入时绑定函数对象，使得对源模块属性的打桩失效。
from ..utils import url_validator
from ..utils.pinned_ip_adapter import PinnedIPHTTPAdapter
import datetime

# 创建日志记录器
logger = setup_logger("generic_downloader")


def _redact_proxy_credentials(proxy_url: str) -> str:
    """去掉代理 URL 里嵌入的 user:pass@ 凭据，仅用于日志输出。

    HTTP(S)_PROXY 环境变量常见写法允许把认证信息直接编码进 URL
    （如 http://user:pass@proxy.internal:3128），DEBUG 日志原样打印
    这个字符串会把代理密码写进持久化日志/日志采集系统。
    """
    try:
        parsed = urlparse(proxy_url)
    except ValueError:
        return proxy_url
    if not parsed.username and not parsed.password:
        return proxy_url
    redacted_netloc = parsed.hostname or ""
    if parsed.port:
        redacted_netloc = f"{redacted_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=redacted_netloc))


class GenericDownloader(BaseDownloader):
    """
    通用下载器，用于处理直接的音视频下载链接
    """
    
    def __init__(self):
        """
        初始化通用下载器
        """
        super().__init__()
        self._cached_video_info: dict[str, dict] = {}
        # 支持的音视频扩展名
        self.supported_audio_extensions = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.wma'}
        self.supported_video_extensions = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v'}
        self.supported_extensions = self.supported_audio_extensions | self.supported_video_extensions

        # 初始化临时文件目录
        temp_dir_config = self.config.get("storage", {}).get("temp_dir", "./data/temp")
        self.temp_dir = os.path.abspath(temp_dir_config)
        # 确保临时目录存在
        os.makedirs(self.temp_dir, exist_ok=True)
        
    def can_handle(self, url):
        """
        判断是否可以处理该URL
        通用下载器作为兜底，可以处理任何URL

        参数:
            url: 视频URL

        返回:
            bool: 总是返回True作为兜底处理器
        """
        return True

    # 重定向最大跟随跳数：既要允许正常的 CDN/短链跳转，又要防止恶意或
    # 异常服务器无限重定向拖垮下载线程
    _MAX_REDIRECTS = 5

    def _validate_or_raise(self, url: str) -> None:
        """
        对 URL 做 SSRF 安全校验（协议白名单 + 私网/回环/链路本地/云元数据拦截 +
        DNS 二次解析校验），失败时转换为面向用户可读的 InvalidURLError。

        GenericDownloader 是兜底处理器（can_handle 恒为 True），任何未被其他
        平台专用下载器识别的 URL 都会落到这里，必须在发起任何网络请求前拦截。

        参数:
            url: 待校验的 URL

        抛出:
            InvalidURLError: URL 指向内网/回环/链路本地/云元数据等不安全地址，
                              或协议不在 http/https 白名单内
        """
        try:
            url_validator.validate_url_safe(url)
        except url_validator.URLValidationError as e:
            logger.error(f"URL 安全校验未通过，已阻止请求: {url}, 原因: {e}")
            raise InvalidURLError("URL 指向内部网络地址，已被安全策略拦截") from e

    def _safe_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        对 URL 做 SSRF 校验后发起请求，并逐跳校验重定向目标。

        不使用 requests 自带的 allow_redirects=True 自动跳转，而是手动跟随并
        在每一跳都重新校验 + 钉定 IP（见 _dispatch_pinned_request），防止
        公网 URL 通过 302 等方式跳转到内网/云元数据地址造成 SSRF 绕过。

        跨跳 Cookie 透传（codex-review P2）：_dispatch_pinned_request 每跳都
        新建一个 requests.Session（详见该方法文档），刻意不经过
        Session.send() 自带的自动重定向/Cookie/Hook 流水线，因此也不会像
        requests 默认那样自动把上一跳响应的 Set-Cookie 带到下一跳请求。部分
        媒体服务器用 302 + Set-Cookie 下发后续下载凭证（重定向目标校验这个
        cookie 才放行），不手动透传会导致这类原本 requests 自动重定向能
        成功的合法链接在这里变成 401/403。这里用一个贯穿本次调用全部跳数的
        RequestsCookieJar 解决：每跳发起前把 jar 里已累积的 cookie 通过
        cookies= 参数带上，每跳响应回来后把它的 Set-Cookie（response.cookies）
        并入 jar，供后续跳使用。调用方若显式传入 cookies，作为初始种子合并
        进这个跨跳 jar（当前代码库内没有调用方这样做，但保留该入口以兼容未
        来调用方）。

        域名匹配在钉定 IP 场景下依然正确：cookies 是在 _dispatch_pinned_request
        内 session.prepare_request() 阶段被编码进 Cookie 请求头的，这一步发生
        在 PinnedIPHTTPAdapter.send() 把 request.url 改写成钉定 IP、把 Host
        头设为真实域名之前——此时 prepared.url 仍是未改写的真实域名 URL，
        requests.cookies.MockRequest.get_host()/get_full_url() 据此算出的
        "请求域名"就是真实域名，不会因为最终连接目标是 IP 字面量而匹配失败
        或被拒绝（unit 测试
        TestCookieCarriedAcrossRedirectHops.test_cookie_domain_matches_real_hostname_despite_pinned_ip_url
        直接断言了这一点，而不是仅凭读源码推断）。响应端同理：
        PinnedIPHTTPAdapter.send() 在改写 request.url/Host 头之后才调用
        super().send()，而 HTTPAdapter.build_response() 正是用这个已改写过
        的 request 对象调用 extract_cookies_to_jar() 提取 Set-Cookie——此时
        request.headers['Host'] 已经是真实域名，MockRequest.get_full_url()
        会优先用它重建"期望的域名"，所以收到的 Set-Cookie 依然被正确归属到
        真实域名，而不是钉定 IP。

        参数:
            method: 'head' 或 'get'
            url: 请求 URL
            **kwargs: 透传给底层请求的参数（如 timeout、stream、headers、
                      cookies）。不要传入 allow_redirects，本方法自己手动
                      处理重定向，从不让底层库自动跳转

        返回:
            requests.Response: 最终（非重定向）响应

        抛出:
            InvalidURLError: 任意一跳未通过 SSRF 校验，或重定向跳数超过上限
        """
        current_url = url
        redirect_count = 0

        cookie_jar = requests.cookies.RequestsCookieJar()
        caller_cookies = kwargs.pop("cookies", None)
        if caller_cookies:
            cookie_jar = requests.cookies.merge_cookies(cookie_jar, caller_cookies)

        while True:
            response = self._dispatch_pinned_request(
                method, current_url, cookies=cookie_jar, **kwargs
            )
            # 合并本跳响应的 Set-Cookie，供下一跳（如果还有）使用
            cookie_jar = requests.cookies.merge_cookies(cookie_jar, response.cookies)

            if response.is_redirect:
                redirect_count += 1
                if redirect_count > self._MAX_REDIRECTS:
                    if kwargs.get("stream"):
                        response.close()
                    raise InvalidURLError(
                        f"重定向次数超过上限（{self._MAX_REDIRECTS}），已终止请求: {url}"
                    )
                next_url = urljoin(current_url, response.headers["Location"])
                logger.info(
                    f"跟随重定向 ({redirect_count}/{self._MAX_REDIRECTS}): "
                    f"{current_url} -> {next_url}"
                )
                if kwargs.get("stream"):
                    response.close()
                current_url = next_url
                continue

            return response

    # 钉定候选 IP 上限：与 url_validator.validate_url_safe_with_ips 的默认值
    # 保持一致（见该函数文档的取舍说明）。这里单独声明一个类常量，而不是
    # 依赖 url_validator 的默认参数，是为了让"最多重试几个候选地址"这个
    # 影响本类请求行为的数字在 generic.py 内可见、可独立调整。
    _MAX_PINNED_IP_CANDIDATES = 3

    def _dispatch_pinned_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        校验 URL 安全性，并把真正发起的网络连接"钉"在校验时已解析、已检查过
        的那个 IP 上，再发起一次请求。

        背景（DNS rebinding TOCTOU）：如果只是校验通过后就把原始 URL（域名
        形式）交给 requests 发起请求，requests/urllib3 会独立地重新解析一次
        DNS —— 攻击者控制的域名完全可能在"校验时解析"和"连接时解析"这两次
        解析之间切换 DNS 记录（先给公网 IP 通过校验，连接时再给内网/云元数据
        IP），从而绕过 SSRF 防护。这里改为：校验函数把它解析并检查过的 IP
        原样返回，网络连接直接使用这个 IP（PinnedIPHTTPAdapter），不再触发
        任何针对该域名的新 DNS 查询，彻底消除这个窗口。

        每次调用构造一个新的 requests.Session（而非复用单例）：Session 本身
        不是线程安全的，本方法可能被并发的下载任务同时调用，per-request 新建
        开销很小，换来无需操心跨线程共享状态。用这个 Session 只为两件事：
        1) 通过 Session.merge_environment_settings 合并部署环境配置
           ——REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE 自定义 CA 证书（verify/
           cert）以及 HTTP(S)_PROXY / NO_PROXY 环境代理（proxies，见下方
           "环境代理"说明，现在会正常透传），2) 作为 PinnedIPHTTPAdapter
           的挂载点，通过 Session.get_adapter 按 URL 前缀取出实际发送请求
           的适配器。不调用 Session.send()/Session.request()：那条路径即使
           传 allow_redirects=False，内部仍会在响应上预取一次下一跳信息用于
           Response.next()（读 Location、抽取 Cookie、重建 auth），这属于
           _safe_request 自己实现的逐跳重定向校验不需要、也不应该由 Session
           重复处理的"自动重定向/Cookie/Hook 流水线"；这里改为拿到 Session
           选中并已合并好环境设置的适配器后，直接调用它的 send()，只借用
           Session 做配置合并与适配器挂载查找，不借用它的响应后处理逻辑。

        环境代理（codex-review R6 #2 引入，经四轮问题排查后于本版本定案为
        "代理 + 钉定 IP + 正确 SNI 三者共存"）——这里的演变分四步：
        1) 最初实现：命中 HTTP(S)_PROXY 时整段跳过 IP 钉定，让 Session 用
           默认适配器按原始域名经代理请求，理由是"代理有自己独立的 DNS
           视角，钉 IP 对它没有意义"。但代理环境在服务端部署很常见，这条
           "跳过"分支等于让 SSRF 防护在代理路径上形同虚设（ci-gate review
           指出的安全问题：攻击者控制的域名可以让本地校验解析出公网 IP、
           代理侧独立解析出内网/云元数据地址，经典 DNS rebinding 绕过）。
        2) 修正为"代理与钉 IP 不互斥"：把 merge 出来的 proxies 连同钉定 IP
           一起透传给 target_adapter.send()，代理收到的 CONNECT/请求目标
           就是钉定 IP。这堵上了安全窟窿，但引入了新的正确性问题（ci-gate
           review 第三轮指出）：requests 对"直连"和"走代理"走的是两套连接
           池实现——PoolManager vs ProxyManager；PinnedIPHTTPAdapter.
           init_poolmanager() 里注入的 server_hostname/assert_hostname 只
           作用于直连 PoolManager，不会传播到 ProxyManager。结果是代理路径
           下 CONNECT 目标虽然是钉定 IP，TLS SNI/证书主机名校验用的参数却
           不对，正常的 HTTPS 域名会因为按错误主机名做证书校验而请求失败
           ——真实的功能回归。
        3) 一度改为"GenericDownloader 完全不使用环境代理配置，始终直连钉定
           IP"（显式丢弃 merge_environment_settings 合并出的 proxies 键），
           理由是"在代理路径里做正确的 SNI 钉定需要覆盖 urllib3
           HTTPAdapter.proxy_manager_for() 之类的内部机制，复杂度与收益不
           成比例"。这个判断站不住：本 PR 的意图是补 SSRF 防护，不应该
           静默移除 requests 标准的环境代理能力——依赖企业代理/容器出网
           代理访问外部媒体的部署会全部失效，属于真实的功能回归（ci-gate
           review 第四轮打回）。
        4) 现在的做法：正面解决 2) 遗留的 SNI/ProxyManager 传播问题，而不
           是绕开代理。PinnedIPHTTPAdapter 新增 proxy_manager_for() 覆写
           （见 utils/pinned_ip_adapter.py），在 super().proxy_manager_for()
           构造/返回 ProxyManager 前注入与 init_poolmanager() 同样的
           server_hostname/assert_hostname——这两个参数经
           ProxyManager.__init__ 的 **connection_pool_kw 一路传到 CONNECT
           隧道内部实际连接目标的 HTTPSConnectionPool，与直连场景的传递
           路径完全对称。下面重新把 merge_environment_settings 合并出的
           完整 settings（含 proxies）合并进 send_kwargs，代理配置正常
           传给 adapter.send()：代理存在时请求经代理转发，CONNECT/转发目标
           是已校验的钉定 IP（SSRF 钉定语义不变），TLS SNI 和证书主机名
           校验仍按真实 hostname 进行（功能正确性也不再回归）。

        多候选 IP 钉定重试（codex-review R8 #2）：双栈/多节点域名解析出的
        多个公网候选地址里，第一条（常见 AAAA 优先）在当前网络恰好不可达
        时，只钉第一条的实现会反复重试同一个死地址——即便同一次解析结果里
        还有其他可达的候选，也永远不会被尝试到。这里改为拿
        validate_url_safe_with_ips 返回的全部候选（最多
        _MAX_PINNED_IP_CANDIDATES 个），按顺序逐个钉定尝试：只有连接类错误
        （requests.exceptions.ConnectionError / Timeout，含其子类
        ConnectTimeout / ReadTimeout）才换下一个候选重试——这类错误说明
        "这个 IP 连不上"，换一个候选是合理的补救；HTTP 4xx/5xx 从不会作为
        异常从 HTTPAdapter.send() 抛出（由调用方对 Response 显式调
        raise_for_status()），SSRF 拒绝发生在候选循环开始之前，两者都不会
        触发换址，保持"服务器给出了明确响应/安全策略已拒绝"这类结果的
        确定性，不会被误当成"网络不可达"重试成另一个地址。

        不与 download_file 外层的下载重试循环（最多 3 次、每次间隔退避）
        叠成 O(n*m) 请求风暴：候选地址本身已经限定在
        _MAX_PINNED_IP_CANDIDATES（3）个以内，候选之间不额外等待——"尝试
        全部候选"在外层看来仍然只是一次尝试；只有当本轮全部候选都失败时，
        才会真正耗尽外层的这一次尝试，交给外层已有的退避重试机制处理，
        最坏情况下总请求数是 3（外层）× 3（候选）= 9 次，仍在合理范围内。

        参数:
            method: 'head' 或 'get'
            url: 请求 URL（尚未做过 SSRF 校验）
            **kwargs: 透传给 HTTPAdapter.send 的参数（timeout、stream 等）；
                      headers、cookies 会被合并进构造出的请求中（cookies 由
                      _safe_request 的跨跳 Cookie Jar 透传，用于承接上一跳
                      响应下发的 Set-Cookie，见其文档"跨跳 Cookie 透传"一节）

        返回:
            requests.Response

        抛出:
            InvalidURLError: URL 未通过 SSRF 校验；或校验时无法获得任何已
                              验证 IP（如 DNS 解析失败）—— fail-closed，不
                              回退为不钉 IP 的普通请求
            requests.exceptions.RequestException: 全部候选 IP 均连接失败
                              时，抛出最后一个候选的原始异常；或非连接类
                              错误直接透传（不换址）
        """
        try:
            _, pinned_ips = url_validator.validate_url_safe_with_ips(
                url, max_candidates=self._MAX_PINNED_IP_CANDIDATES,
            )
        except url_validator.URLValidationError as e:
            logger.error(f"URL 安全校验未通过，已阻止请求: {url}, 原因: {e}")
            raise InvalidURLError("URL 指向内部网络地址，已被安全策略拦截") from e

        if not pinned_ips:
            # fail-closed（codex-review R6 #1）：validate_url_safe_with_ips 对
            # DNS 解析失败曾经历过"放行，可能是瞬时故障"的宽松策略，本方法
            # 过去也照着这个假设回退成不钉 IP 的普通 requests.get()/head()
            # ——但那条回退路径既没有钉 IP，又用的是 requests 的默认行为
            # （自动跟随重定向），等于同时打开了 DNS rebinding 和"跳到私网"
            # 两条 SSRF 绕过通道：攻击者只需要让校验时刻的解析报出一次可控
            # 的临时性错误（如域名先返回 SERVFAIL/超时），就能把本应被拦截
            # 的目标直接放到不设防的请求路径上。
            #
            # 是否存在"宽松放行"的正当场景？评估过一种可能——某些运行环境
            # 的 DNS 解析函数受限（如容器网络策略只允许特定域名解析），会让
            # 合法请求也遇到解析报错。但那属于该环境自身的网络配置问题，
            # 应该在部署层面解决（如修正 DNS/网络策略、把目标域名加入
            # download_url_allowlist），不能反过来放宽 SSRF 边界——安全兜底
            # 优先于"尽量放行"。调用方（_safe_request 及其上层的重试与
            # InvalidURLError 用户可读报错）已经能正确处理这里的拒绝，不会
            # 引入新的、无法诊断的失败模式。
            logger.error(f"DNS 解析失败，无法钉定已校验 IP，按 fail-closed 策略拒绝请求: {url}")
            raise InvalidURLError(f"URL 安全校验无法确认目标 IP，已按安全策略拒绝访问: {url}")

        parsed_url = urlparse(url)
        headers = kwargs.pop("headers", None)
        # 跨跳 Cookie 透传（见 _safe_request 文档）：session.prepare_request()
        # 会把它编码进 prepared.headers['Cookie']，且这一步发生在下方 pin
        # 到 IP 之前，prepared.url 此时仍是真实域名，Cookie 域名匹配按真实
        # 域名进行，不受最终连接目标是 IP 字面量影响。
        cookies = kwargs.pop("cookies", None)

        session = requests.Session()
        try:
            prepared = session.prepare_request(
                requests.Request(method.upper(), url, headers=headers, cookies=cookies)
            )
            # PinnedIPHTTPAdapter.send() 会把 request.url 原地改写成钉定 IP
            # 的形式（见 utils/pinned_ip_adapter.py），供下面多候选重试循环
            # 在每次尝试前重置回未钉定的原始形式——否则第二个候选调用
            # _pin_to_ip() 时会因为看到上一次改写后的 IP（而不是真实
            # hostname）而拒绝发送。
            original_prepared_url = prepared.url

            # IDN（国际化域名）规范化（ci-gate review 发现的 P2 回归）：
            # session.prepare_request() 内部会把非 ASCII 主机名做 IDNA 编码，
            # 规范化为 punycode/A-label 形式（如 "bücher.example" ->
            # "xn--bcher-kva.example"），original_prepared_url 已经是这个
            # 规范化后的形式。但下面构造 PinnedIPHTTPAdapter 时如果继续用
            # parsed_url.hostname（对原始、未规范化的 url 变量做 urlparse，
            # IDN 场景下仍是 Unicode 形式）传入，就会跟 PinnedIPHTTPAdapter.
            # send() 内部拿 prepared.url（punycode）解析出的 hostname 对不
            # 上——_pin_to_ip() 的一致性检查会直接抛 ValueError 拒绝发送，
            # 导致所有 IDN 域名请求在发起前就失败（requests 原生直连本来会
            # 自动做这层规范化，钉 IP 改造把这一步和后续比较拆开后引入的
            # 真实功能回归）。这里统一改为从已规范化的 original_prepared_url
            # 取 hostname，让整条链路（DNS 解析校验用的是原始 hostname，
            # socket.getaddrinfo 对 Unicode 主机名有内置的 IDNA 处理，结果
            # 一致，无需改动）到 PinnedIPHTTPAdapter 构造、到 send() 内部
            # 比较，全程只使用这一种（punycode）形式，不再中途切换编码。
            normalized_hostname = urlparse(original_prepared_url).hostname

            # 合并部署环境配置：REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE 决定的
            # 自定义 CA 证书（verify/cert）。merge_environment_settings 是
            # Session.request() 内部用来做这件事的同一个方法，这里显式调用
            # 以便在不经过 Session.send() 的情况下也能拿到同样的合并结果。
            settings = session.merge_environment_settings(
                prepared.url,
                {},
                kwargs.get("stream"),
                kwargs.get("verify"),
                kwargs.get("cert"),
            )

            # 环境代理（HTTP(S)_PROXY/NO_PROXY）正常生效——见上方 docstring
            # "环境代理"一节演进步骤 4)：PinnedIPHTTPAdapter.
            # proxy_manager_for() 已经把 server_hostname/assert_hostname
            # 正确注入到代理路径使用的 ProxyManager，代理与钉定 IP、正确
            # SNI 三者可以正常共存，不再需要丢弃 merge 出来的 proxies 键。
            env_proxy = settings.get("proxies", {}).get(parsed_url.scheme)
            if env_proxy:
                logger.debug(
                    f"检测到环境代理配置 {parsed_url.scheme}="
                    f"{_redact_proxy_credentials(env_proxy)}，"
                    f"本次请求经该代理转发，CONNECT/转发目标为已校验钉定 IP: {url}"
                )

            send_kwargs = dict(kwargs)
            send_kwargs.update(settings)

            for candidate_index, candidate_ip in enumerate(pinned_ips):
                prepared.url = original_prepared_url

                adapter = PinnedIPHTTPAdapter(
                    hostname=normalized_hostname,
                    pinned_ip=candidate_ip,
                    is_https=(parsed_url.scheme == "https"),
                )
                # 用与请求 URL 匹配的 scheme 前缀挂载，替换 Session 默认/
                # 上一个候选的同前缀适配器；session.get_adapter() 会按最长
                # 前缀匹配选中它。整个 Session 只服务这一次请求，用完即弃
                # （finally 里 session.close() 会级联关闭挂载的适配器），
                # 不会有跨请求的挂载残留风险。
                session.mount(f"{parsed_url.scheme}://", adapter)
                target_adapter = session.get_adapter(prepared.url)

                try:
                    return target_adapter.send(prepared, **send_kwargs)
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                    is_last_candidate = candidate_index == len(pinned_ips) - 1
                    if is_last_candidate:
                        # 全部候选都已连接失败，透传最后一个候选的原始异常，
                        # 交给上层（_safe_request/download_file）已有的重试
                        # 与错误处理逻辑，不在这里吞掉或改写异常类型。
                        raise
                    logger.warning(
                        f"钉定 IP {candidate_ip} 连接失败（{exc.__class__.__name__}），"
                        f"尝试下一个已验证候选地址 "
                        f"({candidate_index + 2}/{len(pinned_ips)}): {url}"
                    )
        finally:
            session.close()

    def _is_media_url(self, url):
        """
        检查URL是否直接指向媒体文件

        参数:
            url: 文件URL

        返回:
            bool: 是否是媒体文件URL

        抛出:
            InvalidURLError: HEAD 探测过程中命中不安全的重定向目标
        """
        try:
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path.lower())

            # 检查URL路径中的文件扩展名
            _, ext = os.path.splitext(path)
            if ext in self.supported_extensions:
                return True

            # 尝试HEAD请求获取Content-Type（经 SSRF 校验 + 逐跳重定向校验）
            try:
                response = self._safe_request("head", url, timeout=10)
                content_type = response.headers.get('Content-Type', '').lower()

                # 检查Content-Type是否是音视频类型
                if any(media_type in content_type for media_type in ['audio/', 'video/']):
                    return True
            except InvalidURLError:
                # SSRF 拦截需要向上抛出终止整个处理流程，不能当作"探测失败"忽略
                raise
            except requests.exceptions.RequestException:
                pass

            return False
        except InvalidURLError:
            raise
        except Exception as e:
            logger.error(f"检查媒体URL失败: {str(e)}")
            return False
    
    def get_video_info(self, url):
        """
        获取视频信息
        对于通用下载器，只返回基本信息
        
        参数:
            url: 视频URL
            
        返回:
            dict: 包含视频信息的字典

        抛出:
            InvalidURLError: URL 未通过 SSRF 安全校验
        """
        logger.info(f"通用下载器处理URL: {url}")

        # SSRF 校验：generic 下载器是兜底处理器，任何 URL 都可能落到这里，
        # 必须先过安全校验再发起任何网络请求（含下方的媒体类型 HEAD 探测）
        self._validate_or_raise(url)

        try:
            cache_id = self.extract_video_id(url)
            if cache_id in self._cached_video_info:
                logger.debug(f"[实例缓存命中] 使用缓存的视频信息: {cache_id}")
                return self._cached_video_info[cache_id]
        except Exception:
            cache_id = None
        
        # 检查是否是直接的媒体文件链接
        if self._is_media_url(url):
            logger.info(f"检测到直接媒体文件链接: {url}")
            
            # 从URL中尝试提取文件名
            parsed_url = urlparse(url)
            path = unquote(parsed_url.path)
            filename = os.path.basename(path)
            
            # 如果没有文件名或文件名不合法，生成一个
            if not filename or not any(filename.endswith(ext) for ext in self.supported_extensions):
                # 根据时间戳生成文件名
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                # 尝试从HEAD请求获取文件类型（经 SSRF 校验 + 逐跳重定向校验）
                ext = '.mp4'  # 默认扩展名
                try:
                    response = self._safe_request('head', url, timeout=10)
                    content_type = response.headers.get('Content-Type', '').lower()
                    # 根据Content-Type确定扩展名
                    if 'audio/mpeg' in content_type or 'audio/mp3' in content_type:
                        ext = '.mp3'
                    elif 'audio/mp4' in content_type or 'audio/m4a' in content_type:
                        ext = '.m4a'
                    elif 'audio/' in content_type:
                        ext = '.mp3'  # 默认音频格式
                    elif 'video/' in content_type:
                        ext = '.mp4'  # 默认视频格式
                except InvalidURLError:
                    # SSRF 拦截需要向上抛出终止整个处理流程，不能静默回退默认扩展名
                    raise
                except requests.exceptions.RequestException:
                    pass
                filename = f"generic_{timestamp}{ext}"
            
            # 返回视频信息
            result = {
                "video_title": "",  # 留空，后续由LLM生成
                "author": "",
                "description": "",
                "download_url": url,
                "filename": filename,
                "platform": "generic",
                "video_id": self.extract_video_id(url),
                "is_generic": True  # 标记为通用下载
            }
            if cache_id:
                self._cached_video_info[cache_id] = result
            return result
        else:
            # 对于非直接媒体链接，尝试作为网页处理
            logger.warning(f"URL不是直接媒体文件链接，尝试作为网页处理: {url}")
            
            # 这里可以添加网页解析逻辑，尝试从网页中提取媒体链接
            # 目前暂时返回错误
            raise ValueError(f"无法处理该URL，不是有效的媒体文件链接: {url}")
    
    def get_subtitle(self, url):
        """
        获取字幕
        通用下载器不支持字幕
        
        参数:
            url: 视频URL
            
        返回:
            None
        """
        return None
    
    def extract_video_id(self, url):
        """
        提取视频ID
        对于通用URL，使用URL哈希作为ID
        
        参数:
            url: 视频URL
            
        返回:
            str: 视频ID
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]
    
    def download_file(self, url, filename):
        """
        下载文件到本地（增强版，支持大文件和断点续传）
        
        参数:
            url: 文件URL
            filename: 本地文件名
            
        返回:
            str: 本地文件路径，如果下载失败则返回None

        抛出:
            InvalidURLError: URL 未通过 SSRF 安全校验（永久性错误，不重试）
        """
        # SSRF 校验：generic 下载器是兜底处理器，任何 URL 都可能落到这里，
        # 必须先过安全校验再发起任何网络请求
        self._validate_or_raise(url)

        # 落到当前任务的专属目录（data/temp/task_<id>/），实现：
        # 1) 任务结束时由 clean_up_task 一并 rmtree，不残留；
        # 2) 不同任务即使同名文件也写在各自目录，避免同名碰撞互相覆盖/误删。
        task_dir = self.temp_manager.get_current_task_dir()
        local_path = os.path.join(str(task_dir), filename)

        # 创建目录（如果不存在）
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        
        # 最大重试次数
        max_retries = 3
        # 重试退避（秒）：给瞬态故障（如文件服务重启的部署窗口）留恢复时间
        retry_backoff = (5, 15)
        chunk_size = 1024 * 1024  # 1MB 块大小
        
        # 尝试导入企微通知器
        try:
            # 使用包内绝对导入，避免重复加载模块导致全局实例被初始化两次
            from ..utils.notifications import WechatNotifier
            wechat_notifier = WechatNotifier()
        except:
            wechat_notifier = None
        
        for attempt in range(max_retries):
            if attempt > 0:
                delay = retry_backoff[min(attempt - 1, len(retry_backoff) - 1)]
                logger.info(f"等待 {delay}s 后重试下载...")
                time.sleep(delay)
            try:
                logger.info(f"开始下载文件 (尝试 {attempt + 1}/{max_retries}): {url}")
                
                # 检查是否已有部分下载的文件
                resume_header = {}
                initial_pos = 0
                
                if os.path.exists(local_path):
                    initial_pos = os.path.getsize(local_path)
                    if initial_pos > 0:
                        resume_header['Range'] = f'bytes={initial_pos}-'
                        logger.info(f"检测到部分下载文件，从 {initial_pos} 字节处续传")
                
                # 发起请求（经 SSRF 校验 + 逐跳重定向校验）
                try:
                    response = self._safe_request(
                        'get',
                        url,
                        headers=resume_header,
                        stream=True,
                        timeout=(30, 300)  # 连接超时30秒，读取超时300秒
                    )
                    response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    # 处理 416 Range Not Satisfiable 错误（服务器不支持断点续传）
                    if e.response.status_code == 416:
                        logger.warning(f"服务器不支持断点续传 (416)，删除部分文件重新下载: {local_path}")
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            logger.info("已删除部分下载文件，准备重新下载")
                        # 重新发起请求（不带 Range header）
                        response = self._safe_request(
                            'get',
                            url,
                            stream=True,
                            timeout=(30, 300)
                        )
                        response.raise_for_status()
                        initial_pos = 0  # 重置初始位置
                        resume_header = {}  # 清空 resume header
                    else:
                        raise
                
                # 获取文件总大小
                content_length = response.headers.get('content-length')
                if content_length:
                    total_size = int(content_length)
                    if initial_pos > 0:
                        total_size += initial_pos
                    logger.info(f"文件总大小: {total_size / (1024*1024):.2f} MB")
                
                # 打开文件进行写入
                mode = 'ab' if initial_pos > 0 else 'wb'
                with open(local_path, mode) as f:
                    downloaded = initial_pos
                    last_log_time = datetime.datetime.now()
                    
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # 每10秒打印一次进度
                            now = datetime.datetime.now()
                            if (now - last_log_time).seconds >= 10:
                                if content_length:
                                    progress = (downloaded / total_size) * 100
                                    progress_msg = f"下载进度: {progress:.1f}% ({downloaded / (1024*1024):.2f}/{total_size / (1024*1024):.2f} MB)"
                                    logger.info(progress_msg)
                                    
                                    # 对于大文件（>20MB），每30%进度发送企微通知
                                    if (total_size > 20 * 1024 * 1024 and 
                                        wechat_notifier and 
                                        progress % 30 < 10 and 
                                        progress > 10):
                                        try:
                                            wechat_notifier.send_text(f"【文件下载进度】\n链接: {url[:50]}...\n{progress_msg}")
                                        except:
                                            pass  # 通知失败不影响下载
                                else:
                                    logger.info(f"已下载: {downloaded / (1024*1024):.2f} MB")
                                last_log_time = now
                
                # 验证文件完整性
                final_size = os.path.getsize(local_path)
                if content_length and final_size != total_size:
                    logger.warning(f"文件大小不匹配: 期望 {total_size}, 实际 {final_size}")
                    # 不删除文件，下次重试时会续传
                    continue
                
                logger.info(f"文件下载成功: {local_path} (大小: {final_size / (1024*1024):.2f} MB)")
                return local_path

            except InvalidURLError:
                # SSRF 拦截是永久性错误（重定向目标不安全），重试无意义，
                # 必须直接向上抛出终止整个下载，不能被当作瞬态故障重试或吞掉
                logger.error(f"下载中止：URL 未通过 SSRF 安全校验 (尝试 {attempt + 1}/{max_retries}): {url}")
                raise

            except requests.exceptions.ChunkedEncodingError as e:
                logger.warning(f"分块编码错误 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试断点续传...")
                    continue
                    
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"连接错误 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试重新连接...")
                    continue
                    
            except requests.exceptions.Timeout as e:
                logger.warning(f"下载超时 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("将尝试重新下载...")
                    continue
                    
            except Exception as e:
                logger.error(f"下载异常 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    continue
                    
        # 所有重试都失败了
        logger.error(f"文件下载失败，已尝试 {max_retries} 次: {url}")
        
        # 清理不完整的文件
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info("已清理不完整的下载文件")
            except:
                pass
                
        return None

    def _fetch_metadata(self, url: str, video_id: str) -> VideoMetadata:
        info = self.get_video_info(url)
        return VideoMetadata(
            video_id=info.get("video_id", video_id),
            platform=info.get("platform", "generic"),
            title=info.get("video_title", ""),
            author=info.get("author", ""),
            description=info.get("description", ""),
            extra={"is_generic": True},
        )

    def _fetch_download_info(self, url: str, video_id: str) -> DownloadInfo:
        info = self.get_video_info(url)
        filename = info.get("filename")
        file_ext = None
        if filename and "." in filename:
            file_ext = filename.rsplit(".", 1)[-1]
        return DownloadInfo(
            download_url=info.get("download_url"),
            file_ext=file_ext,
            filename=filename,
        )
