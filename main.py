"""
YouTube yt-dlp 中转服务
======================
给微信小程序 media-collector 用的「YouTube 视频直链」中继。

为什么需要它：
  YouTube 视频文件带签名防盗，云函数无法直接拿到可下载地址。
  本服务跑在境外主机（Render / Railway 等），用 yt-dlp 解析出
  真实直链后返回给云函数，云函数再经 relay 缓存到腾讯云存储，
  小程序即可在国内直接播放/保存。

接口：
  GET /api/info?url=<YouTube链接>
  返回 JSON：
    {
      "success": true,
      "title": "...",
      "author": "...",
      "thumbnail": "https://...jpg",
      "duration": 123,
      "videoUrl": "https://rN---sn-xxx.googlevideo.com/videoplayback?..."
    }
"""

import os
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


@app.get("/api/info")
def info(url: str = Query(...)):
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "best",
            "http_headers": {"User-Agent": UA},
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=False)
        video_url = pick_playable(data)
        if not video_url:
            return {"success": False, "error": "未找到可用的视频直链"}
        return {
            "success": True,
            "title": data.get("title"),
            "author": data.get("uploader") or data.get("channel"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
            "videoUrl": video_url,
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


def _is_googlevideo(url):
    """仅允许代理 googlevideo.com 的视频流，防止本服务被当成开放代理滥用。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host == "googlevideo.com" or host.endswith(".googlevideo.com")
    except Exception:
        return False


@app.get("/api/proxy")
def proxy(url: str = Query(...)):
    """视频流代理：服务端（境外 Render）直连 googlevideo 拉取并流式回传。

    为什么需要：Cloudflare 中继的出口 IP 被 Google CDN 按 IP 风控（403），
    云函数无法经中继直接抓 googlevideo 文件。改为让本服务（境外 IP，可直连
    Google）下载视频流并转发，云函数再经中继（能正常访问 Render）把视频缓存到
    腾讯云存储，从而在国内可播放/保存。
    """
    if not _is_googlevideo(url):
        return {"success": False, "error": "仅允许代理 googlevideo.com 视频流"}
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://www.youtube.com/",
            "Accept": "*/*",
        },
    )
    try:
        upstream = urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        return {"success": False, "error": "上游拉取失败: " + str(e)[:200]}
    ctype = upstream.headers.get("Content-Type") or "video/mp4"

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


@app.get("/")
def root():
    return {"ok": True, "service": "youtube-relay"}
