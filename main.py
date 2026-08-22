"""
YouTube yt-dlp 中转服务 + Instagram GraphQL 无 Cookie 解析
==================================================
给微信小程序 media-collector 用的媒体中继。

功能：
  1. GET /api/info?url=<YouTube链接>  —— yt-dlp 解析 YouTube 视频直链
  2. GET /api/instagram?url=<IG链接>   —— GraphQL 无 cookie 解析 IG 帖子/Reel
  3. GET /api/proxy?url=<媒体URL>       —— 代理 googlevideo / IG 图床流
"""

import os
import re
import json
import tempfile
import urllib.request
import urllib.parse
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# 部署版本标记：用于确认 Render 是否真正拉取了最新代码
VERSION = "v6b-ig-debug"


def header_cookie_to_netscape(header_str, domain=".youtube.com"):
    """把 Cookie-Editor 的 Header 串（"k=v; k2=v2"）转成 Netscape cookies.txt 文本。

    yt-dlp 只有在把 cookie 解析进自己的 cookie jar（即 --cookiefile / Netscape 格式）
    时，才会自动为 YouTube 计算并附带 SAPISIDHASH 签名头；若仅以 http_headers 的
    Cookie 注入，YouTube 仍会判定为机器人（"Sign in to confirm you're not a bot"）。
    因此无论用户给的是 Header 串还是 Netscape，统一落盘为 cookiefile 最稳妥。

    字段分隔符用 chr(9)（真正的 TAB）——源码里直接写 "\\t" 在某些部署/编辑环节会被
    当成字面反斜杠+t，导致 yt-dlp 报 invalid Netscape format。
    """
    TAB = chr(9)
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html"]
    # http.cookiejar 的 _really_load 断言 domain_specified == initial_dot：
    # 域名以 "." 开头（initial_dot=True）时，domain_specified 字段必须为 "TRUE"，
    # 否则触发 AssertionError 被 yt-dlp 包装成 "invalid Netscape format"。
    domain_specified = "TRUE" if domain.startswith(".") else "FALSE"
    for pair in header_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name, value = name.strip(), value.strip().strip('"')
        lines.append(TAB.join([domain, domain_specified, "/", "FALSE", "0", name, value]))
    return "\n".join(lines) + "\n"


def pick_playable(data):
    """从 yt-dlp 解析结果里选一个「音画同流」的可直接播放地址（优先 mp4）。"""
    formats = data.get("formats") or []
    # 1) 优先：带音频+视频的 progressive mp4（小程序 <video> 可直接播/存）
    prog = [
        f for f in formats
        if f.get("ext") == "mp4"
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") not in (None, "none")
        and f.get("url")
    ]
    if prog:
        prog.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        return prog[0]["url"]
    # 2) 退而求其次：任意带音频+视频的格式
    both = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") not in (None, "none")
        and f.get("url")
    ]
    if both:
        both.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        return both[0]["url"]
    # 3) 兜底：返回的顶层 url（可能是纯视频流，无音轨）
    return data.get("url")


# YouTube 反爬升级后，默认 web 客户端常被要求 "Sign in to confirm you're not a bot"。
# 改用非网页 player_client 回退（tv/ios/android/web_safari 等多端客户端），
# 多数情况下无需登录 Cookie 即可拿到直链。仍失败时可经 cookies 参数喂登录态。
PLAYER_CLIENTS = ["tv", "ios", "android", "web_safari", "web_embed", "web"]


@app.get("/api/info")
def info(url: str = Query(...), cookies: str = Query(None)):
    """解析 YouTube 直链。

    cookies: 可选，登录态 cookie。支持两种格式，自动识别并统一转为 Netscape cookiefile：
              - Netscape 格式（浏览器扩展导出的 cookies.txt 内容，含 "# Netscape" 头）
              - Header 串格式（Cookie-Editor 的 "Copy as Header String"，形如 "k=v; k2=v2"）
              必须用 cookiefile（而非仅 http_headers），yt-dlp 才会自动计算 YouTube
              所需的 SAPISIDHASH 签名头，绕过 "Sign in to confirm you're not a bot"。
             也可通过环境变量 YT_COOKIES 统一配置（无需每次请求携带）。
    """
    # 优先用请求参数里的 cookies；否则回退到环境变量 YT_COOKIES（Render 后台配置）
    if not cookies or not cookies.strip():
        cookies = os.environ.get("YT_COOKIES") or ""
    tmp_cookie = None
    try:
        # 有 cookie：优先用 web 客户端——它会返回 progressive 渐进式格式（18/22 等，
        # 音画同流，小程序 <video> 可直接播/存）；cookie 经 SAPISIDHASH 自动绕过 bot 墙。
        # 无 cookie：退回 tv/ios 等非网页客户端，尽量免登录拿到直链。
        player_clients = (
            ["web", "web_safari", "android", "tv", "ios"]
            if cookies and cookies.strip()
            else PLAYER_CLIENTS
        )
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "best",
            "http_headers": {"User-Agent": UA},
            "nocheckcertificate": True,
            "extractor_args": {"youtube": {"player_client": player_clients}},
        }
        # 可选：把 cookie 交给 yt-dlp。统一落盘为 Netscape cookiefile——
        # 这样 yt-dlp 才会自动计算并附带 YouTube 所需的 SAPISIDHASH 签名头，
        # 否则仅以 Cookie 请求头注入仍会被判机器人。
        #   - 已是 Netscape 格式（"# Netscape" 开头 / 含 tab 或换行）→ 直接落盘
        #   - Header 串格式（Cookie-Editor "Copy as Header String"，"k=v; k2=v2"）
        #     → 先转成 Netscape 再落盘
        if cookies and cookies.strip():
            text = cookies.strip()
            if text.startswith("#") or "\t" in text or "\n" in text:
                cookie_text = text
            else:
                cookie_text = header_cookie_to_netscape(text)
            fd, tmp_cookie = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(cookie_text)
            ydl_opts["cookiefile"] = tmp_cookie
        # format="best" 触发带 cookie 的 player API 路径（绕过 bot 墙）。
        # 抓全量格式后用 pick_playable 挑「音画同流」的渐进式 mp4；
        # 个别视频没有渐进式直链时，再强制回退到 360p 渐进式格式 18（几乎所有公开视频都有）。
        video_url = None
        last_err = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                data = ydl.extract_info(url, download=False)
            video_url = pick_playable(data)
        except Exception as e:
            last_err = e
        if not video_url:
            try:
                with yt_dlp.YoutubeDL({**ydl_opts, "format": "18"}) as ydl:
                    data = ydl.extract_info(url, download=False)
                video_url = pick_playable(data) or data.get("url")
            except Exception as e:
                last_err = last_err or e
        if not video_url:
            return {"success": False, "error": str(last_err)[:300], "version": VERSION}
        return {
            "success": True,
            "title": data.get("title"),
            "author": data.get("uploader") or data.get("channel"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
            "videoUrl": video_url,
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:300], "version": VERSION}
    finally:
        if tmp_cookie and os.path.exists(tmp_cookie):
            try:
                os.remove(tmp_cookie)
            except Exception:
                pass


def _is_googlevideo(url):
    """仅允许代理 googlevideo.com 的视频流，防止本服务被当成开放代理滥用。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host == "googlevideo.com" or host.endswith(".googlevideo.com")
    except Exception:
        return False


def _is_instagram_cdn(url):
    """仅允许代理 Instagram 官方图床（scontent / fbcdn），用于把被墙/被风控的
    IG 原图经境外 Render 抓回再缓存到云存储。严禁放通其他域名，避免开放代理滥用。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return (
            host == "scontent.cdninstagram.com"
            or host.endswith(".cdninstagram.com")
            or host == "fbcdn.net"
            or host.endswith(".fbcdn.net")
        )
    except Exception:
        return False


IG_UA = "Instagram 319.0.0.33.90 Android (24/7.0; 640dpi; 1440x2560; samsung; SM-G965F; star2qltecs; samsung; en_US; 519113708)"


@app.get("/api/proxy")
def proxy(url: str = Query(...)):
    """通用媒体代理：服务端（境外 Render）直连上游拉取并流式回传。

    放行两类域名（均严格白名单，防止开放代理滥用）：
      - googlevideo.com：YouTube 视频流（Cloudflare 中继被 Google CDN 按 IP 风控 403，
        改由本服务境外 IP 直连抓取后转发）。
      - scontent*.cdninstagram.com / *.fbcdn.net：Instagram 原图。云函数所在链路经
        Cloudflare 中继取 IG 原图同样被 Meta 按出口 IP 风控 403，且大陆直连被墙；
        改由本服务（境外 IP 未被 Meta 封禁）抓取，云函数再经中继把图缓存到腾讯云。
    """
    if _is_googlevideo(url):
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.youtube.com/",
            "Accept": "*/*",
        }
    elif _is_instagram_cdn(url):
        headers = {
            "User-Agent": IG_UA,
            "Referer": "https://www.instagram.com/",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        }
    else:
        return {"success": False, "error": "仅允许代理 googlevideo.com 与 Instagram 图床(scontent/fbcdn)"}
    req = urllib.request.Request(url, headers=headers)
    try:
        upstream = urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        return {"success": False, "error": "上游拉取失败: " + str(e)[:200]}
    ctype = upstream.headers.get("Content-Type") or "application/octet-stream"

    def gen():
        try:
            while True:
                chunk = upstream.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type=ctype)


# ──────────────────────────────────────────────────────────────────
# Instagram GraphQL 无 Cookie 解析
# ──────────────────────────────────────────────────────────────────
# 原理：Instagram 的 GraphQL 端点（/api/graphql）可以用固定的 doc_id
# 查询帖子媒体，不需要登录 cookie。只需要：
#   - User-Agent（任何浏览器 UA）
#   - X-IG-App-ID（公开固定值 936619743392459，不是密钥，不会过期）
#   - lsd token（从帖子页面 HTML 抓取，每次会变，但不是登录凭证）
#
# 参考：https://github.com/ahmedrangel/instagram-media-scraper (Method 2)

IG_APP_ID = "936619743392459"
IG_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def _extract_shortcode(url):
    """从 Instagram URL 提取 shortcode。支持 /p/xxx /reel/xxx /reels/xxx /tv/xxx"""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _scrape_lsd(shortcode):
    """从 Instagram 帖子页面 HTML 抓取 fresh lsd token（CSRF 标识，非登录凭证）。"""
    page_url = f"https://www.instagram.com/p/{shortcode}/"
    req = urllib.request.Request(page_url, headers={
        "User-Agent": IG_BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "none",
    })
    try:
        r = urllib.request.urlopen(req, timeout=15)
        html = r.read().decode("utf-8", errors="replace")
        # lsd token 通常在 "LSD":{"token":"..."} 或 "lsd":"..." 里
        m = re.search(r'"LSD"\s*,\s*\[\s*\{"token"\s*:\s*"([^"]+)"', html)
        if not m:
            m = re.search(r'"lsd"\s*:\s*"([^"]+)"', html)
        if not m:
            m = re.search(r'"LSD"\s*:\s*\{"token"\s*:\s*"([^"]+)"', html)
        return m.group(1) if m else None
    except Exception:
        return None


def _graphql_query(shortcode, lsd):
    """向 Instagram GraphQL 端点发起查询，返回解析后的 JSON。"""
    variables = json.dumps({"shortcode": shortcode})
    # 参数放在 query string 上（而非 body），这是该端点的约定
    full_url = (
        f"https://www.instagram.com/api/graphql"
        f"?variables={urllib.parse.quote(variables)}"
        f"&doc_id=10015901848480474"
        f"&lsd={urllib.parse.quote(lsd)}"
    )
    req = urllib.request.Request(full_url, data=b"", method="POST", headers={
        "User-Agent": IG_BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-IG-App-ID": IG_APP_ID,
        "X-FB-LSD": lsd,
        "X-ASBD-ID": "129477",
        "Sec-Fetch-Site": "same-origin",
        "Accept": "*/*",
    })
    r = urllib.request.urlopen(req, timeout=15)
    return json.loads(r.read().decode())


def _parse_ig_media(media):
    """把 GraphQL 返回的 xdt_shortcode_media 解析成统一的 mediaList。"""
    if not media:
        return None
    media_list = []
    typename = media.get("__typename", "")
    is_video = media.get("is_video", False)

    # 单图/单视频
    def push_node(node):
        node_is_video = node.get("is_video", False)
        img_url = node.get("display_url") or ""
        thumb = node.get("thumbnail_src") or img_url
        if node_is_video and node.get("video_url"):
            media_list.append({"url": node["video_url"], "type": "video", "thumbnail": thumb})
        elif img_url:
            media_list.append({"url": img_url, "type": "image", "thumbnail": thumb})

    # 多图轮播
    sidecar = media.get("edge_sidecar_to_children")
    if sidecar and sidecar.get("edges"):
        for edge in sidecar["edges"]:
            push_node(edge.get("node", {}))
    else:
        push_node(media)

    if not media_list:
        return None

    caption_edges = media.get("edge_media_to_caption", {}).get("edges", [])
    caption = caption_edges[0]["node"]["text"] if caption_edges else ""
    owner = media.get("owner", {}).get("username", "")

    return {
        "mediaList": media_list,
        "title": caption[:80] if caption else "Instagram帖子",
        "author": f"@{owner}" if owner else "",
    }


@app.get("/api/instagram")
def instagram(url: str = Query(...), debug: str = Query(None)):
    """Instagram 无 Cookie 解析（GraphQL 法）。

    不需要任何登录 cookie，通过 IG 的 GraphQL 端点直接查询公开帖子媒体。
    支持单图、视频、多图轮播（carousel）。

    流程：
      1. 从 URL 提取 shortcode
      2. 先试硬编码 lsd token（快，但可能过期）
      3. 失败则从帖子页面 HTML 抓取 fresh lsd 后重试
      4. 解析 GraphQL 响应，返回统一的 mediaList
    """
    shortcode = _extract_shortcode(url)
    if not shortcode:
        return {"success": False, "error": "无法从 URL 提取 shortcode", "version": VERSION}

    # 尝试用的 lsd token 列表：先试硬编码（快），再试从页面抓取的（新鲜）
    LSD_FALLBACK = "AVqbxe3J_YA"
    lsds_to_try = [LSD_FALLBACK]

    result = None
    debug_info = {"shortcode": shortcode, "lsds_tried": [], "graphql_responses": []}
    for i, lsd in enumerate(lsds_to_try):
        debug_info["lsds_tried"].append(lsd)
        try:
            j = _graphql_query(shortcode, lsd)
            if debug:
                debug_info["graphql_responses"].append(json.dumps(j)[:800])
            media = (j.get("data") or {}).get("xdt_shortcode_media")
            if media:
                result = _parse_ig_media(media)
                if result:
                    break
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            debug_info["graphql_responses"].append(f"HTTP {e.code}: {body}")
            # 403/429 = lsd 过期或被限流，尝试抓 fresh lsd
            if e.code in (403, 429) and i == 0:
                fresh_lsd = _scrape_lsd(shortcode)
                if fresh_lsd and fresh_lsd != LSD_FALLBACK:
                    lsds_to_try.append(fresh_lsd)
                continue
            if debug:
                return {"success": False, "error": f"HTTP {e.code}: {body}", "debug": debug_info, "version": VERSION}
            return {"success": False, "error": f"HTTP {e.code}: {body}", "version": VERSION}
        except Exception as e:
            debug_info["graphql_responses"].append(f"ERR: {str(e)[:300]}")
            if i == 0:
                # 未知错误也尝试抓 fresh lsd
                fresh_lsd = _scrape_lsd(shortcode)
                if fresh_lsd and fresh_lsd != LSD_FALLBACK:
                    lsds_to_try.append(fresh_lsd)
                continue
            if debug:
                return {"success": False, "error": str(e)[:200], "debug": debug_info, "version": VERSION}
            return {"success": False, "error": str(e)[:200], "version": VERSION}

    if not result:
        # 最后尝试：主动抓 fresh lsd 再试一次
        fresh_lsd = _scrape_lsd(shortcode)
        if fresh_lsd and fresh_lsd not in lsds_to_try:
            debug_info["lsds_tried"].append(fresh_lsd)
            try:
                j = _graphql_query(shortcode, fresh_lsd)
                if debug:
                    debug_info["graphql_responses"].append(json.dumps(j)[:800])
                media = (j.get("data") or {}).get("xdt_shortcode_media")
                if media:
                    result = _parse_ig_media(media)
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:300]
                debug_info["graphql_responses"].append(f"HTTP {e.code}: {body}")
            except Exception as e:
                debug_info["graphql_responses"].append(f"ERR: {str(e)[:300]}")

    if not result:
        if debug:
            return {"success": False, "error": "GraphQL 查询返回空数据", "debug": debug_info, "version": VERSION}
        return {"success": False, "error": "GraphQL 查询返回空数据，帖子可能不存在或为私密账户", "version": VERSION}

    return {"success": True, **result, "version": VERSION}


@app.get("/")
def root():
    return {"ok": True, "service": "youtube-relay", "version": VERSION}
