"""Bounded media fetches from public Douyin/CDN addresses. Signed URLs stay in memory."""
import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from .store import InputError

HOSTS=("douyin.com","douyinvod.com","byteimg.com","douyinpic.com","ibytedtos.com","bytecdn.cn","pstatp.com","snssdk.com","bytefcdnrd.com","bytegecko.com")


def validate_url(url):
    p=urlparse(url)
    if p.scheme!="https" or not p.hostname or p.username or p.password or p.port not in (None,443) or not any(p.hostname==h or p.hostname.endswith("."+h) for h in HOSTS):
        raise InputError("媒体地址不属于支持的抖音公开资源域名")
    # TUN DNS proxies map approved public CDN names into RFC 2544's benchmark range.
    # This exception is only for the strict host allowlist above, never RFC1918/loopback.
    fake_dns=ipaddress.ip_network("198.18.0.0/15")
    if any(not ipaddress.ip_address(a[4][0]).is_global and ipaddress.ip_address(a[4][0]) not in fake_dns for a in socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)):
        raise InputError("媒体地址不能指向本机或内网")


class Redirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def open_media(url):
    validate_url(url)
    try:
        return build_opener(Redirect()).open(Request(url,headers={"Referer":"https://www.douyin.com/","User-Agent":"Mozilla/5.0"}),timeout=20)
    except HTTPError as error:
        code=error.code;error.close()
        raise InputError(f"资源响应 HTTP {code}，请刷新作品后重试") from None
    except URLError:
        raise InputError("资源连接失败，请检查网络后重试") from None


def read_public(url,maximum,image=False):
    with open_media(url) as response:
        mime=response.headers.get_content_type()
        if image and mime not in ("image/jpeg","image/png","image/webp","image/avif"):
            raise InputError("封面响应不是支持的图片")
        data=response.read(maximum+1)
    if not data or len(data)>maximum:raise InputError("资源为空或超过大小限制")
    return data,mime


def download_video(url,path,progress):
    temporary=path.with_suffix(".part")
    total=0
    with open_media(url) as response, temporary.open("xb") as output:
        mime=response.headers.get_content_type()
        if not (mime.startswith("video/") or mime=="application/octet-stream"):
            raise InputError("返回内容不是视频，可能需要重新登录或验证")
        length=response.headers.get("Content-Length", "")
        expected=int(length) if str(length).isdigit() and int(length)>0 else None
        if expected and expected>500*1024*1024:raise InputError("视频超过 500 MB，已停止下载")
        progress(0,expected)
        while chunk:=response.read(1024*1024):
            if total==0 and not (b"ftyp" in chunk[:32] or chunk.startswith(b"\x1aE\xdf\xa3")):
                raise InputError("未识别到有效 MP4/WebM 文件头，未标记下载成功")
            total+=len(chunk)
            if total>500*1024*1024:raise InputError("视频超过 500 MB，已停止下载")
            output.write(chunk)
            progress(total,expected)
        if expected and total!=expected:raise InputError("视频下载不完整，已保留临时文件，请重试")
    if not total:raise InputError("下载为空，未标记成功")
    temporary.rename(path)
