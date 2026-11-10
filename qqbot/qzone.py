"""QQ 空间（说说）读写。

走 Qzone 的网页接口：
- cookie 由协议端的 `get_cookies` 提供（domain=user.qzone.qq.com）
- g_tk 直接用返回的 `bkn`，不用自己从 skey 算
- 发说说前要先从个人主页取 `qzonetoken`

几点必须知道：
1. 这是**腾讯的内部接口**，没有文档，随时可能变；
2. 操作的是**真实账号**，发出去的说说真的会被好友看到；
3. 频率别高，一天一两条就够了，多了容易触发风控。

    from qqbot.qzone import Qzone
    qz = Qzone(onebot, "10002")
    await qz.publish("今天也在努力吃白饭")
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
# 读一条说说下的评论（只读）：和发说说同一套 cookie + g_tk。
DETAIL_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
    "/cgi-bin/emotion_cgi_msgdetail_v6"
)
# 回一条评论（给某条说说发评论）：走 feeds 那条接口。
# `emotion_cgi_reply_v6` 那套老参数 2026-10-27 实测发不出去，别再混着用。
COMMENT_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_re_feeds"
)
# 删除没有走网页接口：emotion_cgi_delete_v6 缺 fid，怎么传都 -10004。
# 改成调协议端的 delete_qzone_msg，见 Qzone.delete()
PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
HOME_URL = "https://user.qzone.qq.com/{uin}"
log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r'window\.g_qzonetoken\s*=\s*"([^"]+)"')
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class QzoneError(RuntimeError):
    pass


def _strip_callback(text: str) -> str:
    """接口返回有的是 JSON，有的是 `_Callback({...});`，统一剥成 JSON。"""
    body = text.strip()
    match = re.match(r"^\s*\w+\((.+?)\)\s*;?\s*$", body, re.S)
    if match:
        return match.group(1)
    return body


def _parse_result(text: str) -> dict[str, Any]:
    """接口有时返回 JSON，有时返回 `_Callback({...})`，两种都兜住。"""
    body = _strip_callback(text)
    code = None
    m = re.search(r'"code"\s*:\s*(-?\d+)', body)
    if m:
        code = int(m.group(1))
    elif '"ret":0' in body or '"ret": 0' in body:
        code = 0
    if code is None and body.startswith("{"):
        code = 0  # 没有 code 字段但返回了对象，当成功
    return {"ok": code == 0, "code": code, "raw": text[:300]}


def _bkn_from_skey(skey: str) -> int:
    """从 skey 算 g_tk。

    NapCat 的 `get_cookies` 会直接给 `bkn`，但 SnowLuma 只给 cookie 字符串、
    不给 bkn —— 没有它所有空间请求都带 `g_tk=0`，一律失败。算法就是腾讯那套
    djb2 变体，跟浏览器里 qzone 用的 g_tk 一致。
    """
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def _as_dict(cookies: Any) -> dict[str, str]:
    """协议端不同版本返回的 cookie 可能是 dict，也可能是 `a=b; c=d` 字符串。"""
    if isinstance(cookies, dict):
        return {str(k): str(v) for k, v in cookies.items()}
    out: dict[str, str] = {}
    if isinstance(cookies, str):
        for part in cookies.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
    return out


_EMOJI_RE = re.compile(r"\[em\][^\[]*\[/em\]")


def _clean_comment(text: str) -> str:
    """评论正文只留人话：QQ 表情码、HTML 实体、多余空白都清掉。"""
    body = _EMOJI_RE.sub("", text or "")
    body = html.unescape(body).replace("\u3000", " ")
    return " ".join(body.split())


def _comment_ts(item: dict[str, Any]) -> int:
    """评论时间戳。

    实测 msgdetail 里三个字段并存：`create_time`（epoch 数字）、
    `createTime2`（"2026-10-27 15:58:09"）、`createTime`（"15:58"）。
    优先用 epoch；都没有就返回 0（调用方会当成"不知道多旧"，不做年龄过滤）。
    """
    raw = item.get("create_time")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    text = str(item.get("createTime2") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _parse_comments(raw_items: Any) -> list[dict[str, Any]]:
    """把 msgdetail 的 commentlist 归一成 [{id, uin, name, text, ts}]。

    只留非空正文：纯表情/纯图片的评论她也没法接话，不如当没看见。
    评论自己的编号在 `tid` 里（int，按说说内递增），不是 `commentid`。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return out
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text_only = _clean_comment(str(item.get("content") or ""))
        if not text_only:
            continue
        out.append(
            {
                "id": str(
                    item.get("tid")
                    or item.get("commentid")
                    or item.get("id")
                    or ""
                ),
                "uin": str(item.get("uin") or ""),
                "name": str(item.get("name") or "").strip(),
                "text": text_only,
                "ts": _comment_ts(item),
            }
        )
    return out


def _to_jpeg(raw: bytes) -> bytes | None:
    """把 webp/gif 之类转成 jpg，Qzone 的图片服务只认常规格式。"""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=92)
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


class Qzone:
    def __init__(self, onebot: Any, uin: str, private: int = 0) -> None:
        self.onebot = onebot
        self.uin = str(uin)
        self.private = private
        self._auth: tuple[dict[str, str], int] | None = None
        self._auth_at = 0.0

    async def _cookies(self) -> tuple[dict[str, str], int]:
        if self._auth and time.time() - self._auth_at < 600:
            return self._auth
        resp = await self.onebot.call("get_cookies", {"domain": "user.qzone.qq.com"})
        data = (resp or {}).get("data") or {}
        cookies = _as_dict(data.get("cookies"))
        if not cookies:
            raise QzoneError("拿不到 Qzone cookie：小号可能没登录过空间，或协议端版本不支持")

        # qzone 的代理接口（说说列表/发说说）认的是 p_skey，不是 skey。
        # NapCat 会给带 p_skey 的 cookie 串；SnowLuma 只给通用那串（没有 p_skey），
        # 于是这些接口全返回空 body（code=None、raw 空）。访问一次空间主页，
        # 让服务端把 p_skey 通过 Set-Cookie 下发，再合并进来。
        if not cookies.get("p_skey"):
            extra = await self._fetch_pskey(cookies)
            if extra:
                cookies.update(extra)

        bkn = int(data.get("bkn") or 0)
        if not bkn:
            # 没有 bkn 就自己算：NapCat 给，SnowLuma 不给。
            # 有 p_skey 时必须用它算，skey 算出来的 g_tk 对不上。
            bkn = _bkn_from_skey(cookies.get("p_skey") or cookies.get("skey") or "")
        self._auth = (cookies, bkn)
        self._auth_at = time.time()
        return self._auth

    async def _fetch_pskey(self, cookies: dict[str, str]) -> dict[str, str]:
        """访问空间主页，把 Set-Cookie 里的 p_skey 捡回来（只读，不改任何东西）。"""
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
                async with session.get(
                    HOME_URL.format(uin=self.uin),
                    cookies=cookies,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    jar = getattr(resp, "cookies", None) or {}
                    picked = {
                        key: morsel.value
                        for key, morsel in jar.items()
                        if key in {"p_skey", "skey", "uin", "pt2gguin"}
                    }
        except Exception as exc:  # noqa: BLE001 - 拿不到就按原样继续，让调用方报错
            log.debug("取 p_skey 失败：%s", exc)
            return {}
        return picked

    async def check(self) -> dict[str, Any]:
        """只检查能不能拿到 cookie / token，不发任何东西。"""
        cookies, bkn = await self._cookies()
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            token = await self._qzonetoken(session, cookies)
        return {
            "cookies": sorted(cookies.keys()),
            "bkn": bkn,
            "qzonetoken": bool(token),
        }

    async def _qzonetoken(self, session: aiohttp.ClientSession, cookies: dict[str, str]) -> str:
        try:
            async with session.get(
                HOME_URL.format(uin=self.uin), cookies=cookies, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                html = await resp.text()
        except Exception:  # noqa: BLE001
            return ""
        m = TOKEN_RE.search(html)
        return m.group(1) if m else ""

    async def feed_list(self, num: int = 5) -> dict[str, Any]:
        """读自己的说说列表（只读，用来验证 cookie 是否有效）。"""
        cookies, bkn = await self._cookies()
        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
            "/cgi-bin/emotion_cgi_msglist_v6"
        )
        params = {
            "uin": self.uin,
            "ftype": "0",
            "sort": "0",
            "pos": "0",
            "num": str(num),
            "replynum": "0",
            "g_tk": str(bkn),
            "callback": "_preloadCallback",
            "code_version": "1",
            "format": "json",
            "need_private_comment": "1",
        }
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            async with session.get(
                url,
                params=params,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()

        body = _strip_callback(text)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "raw": text[:200]}
        msgs = data.get("msglist") or []
        items = [
            {
                "tid": str(m.get("tid") or ""),
                "fid": str(m.get("fid") or ""),
                "content": str(m.get("content") or ""),
                "created": m.get("created_time"),
                "secret": m.get("secret"),
                "has_image": bool(m.get("pic")),
            }
            for m in msgs
        ]
        return {
            "ok": data.get("code") == 0 or "msglist" in data,
            "code": data.get("code"),
            "count": len(msgs),
            "items": items,
            "titles": [item["content"][:30] for item in items[:3]],
        }

    async def delete(self, tid: str, fid: str = "") -> dict[str, Any]:
        """删一条说说。

        走协议端的 `delete_qzone_msg` —— 网页那个 `emotion_cgi_delete_v6`
        怎么传都报 `-10004 参数错误`（它的 fid 在 feed 里根本拿不到）。
        tid 从 feed_list() 里取，别手写。
        """
        if not tid:
            raise QzoneError("没给 tid")
        try:
            resp = await self.onebot.call("delete_qzone_msg", {"tid": tid}, timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除说说调用失败 tid=%s: %s", tid, exc)
            return {"ok": False, "code": None, "raw": str(exc)[:200]}

        ok = (resp or {}).get("status") == "ok" and (resp or {}).get("retcode") == 0
        if ok:
            log.info("说说已删除: tid=%s", tid)
        else:
            log.warning("删除说说失败 tid=%s resp=%s", tid, str(resp)[:200])
        return {"ok": ok, "code": (resp or {}).get("retcode"), "raw": str(resp)[:300]}

    @staticmethod
    def _find(obj: Any, keys: tuple[str, ...]) -> Any:
        """上传返回的字段可能嵌在任意层，递归找。"""
        if isinstance(obj, dict):
            for k in keys:
                if k in obj and obj[k] not in (None, ""):
                    return obj[k]
            for v in obj.values():
                got = Qzone._find(v, keys)
                if got not in (None, ""):
                    return got
        elif isinstance(obj, list):
            for v in obj:
                got = Qzone._find(v, keys)
                if got not in (None, ""):
                    return got
        return None

    @staticmethod
    def _build_richval(payload: dict[str, Any]) -> str:
        """richval 是逗号拼的 9 段：,albumid,lloc,sloc,type,h,w,,h,w"""
        albumid = str(Qzone._find(payload, ("albumid", "album_id")) or "").strip()
        lloc = str(Qzone._find(payload, ("lloc",)) or "").strip()
        sloc = str(Qzone._find(payload, ("sloc",)) or "").strip()
        image_type = str(Qzone._find(payload, ("type", "phototype")) or "").strip() or "1"
        height = str(Qzone._find(payload, ("height", "h")) or "").strip() or "0"
        width = str(Qzone._find(payload, ("width", "w")) or "").strip() or "0"
        if not (albumid and lloc and sloc):
            return ""
        return ",".join(["", albumid, lloc, sloc, image_type, height, width, "", height, width])

    async def upload_image(self, path: Path, title: str = "") -> dict[str, Any]:
        """把一张图传到 Qzone，拿到 richval。

        关键：这个接口收的是 **urlencoded 表单 + base64 的 picfile**，
        不是 multipart 文件上传 —— 用 multipart 会一直挂到网关超时（504）。
        webp 先转 jpg，Qzone 不认 webp。
        """
        raw = path.read_bytes()
        name = path.name
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}:
            converted = _to_jpeg(raw)
            if converted is None:
                raise QzoneError("图片转换失败")
            raw, name = converted, path.stem + ".jpg"

        cookies, bkn = await self._cookies()
        referrer = f"https://user.qzone.qq.com/{self.uin}"
        skey = cookies.get("skey", "")
        p_skey = cookies.get("p_skey", "")
        data = {
            "filename": name,
            "uin": self.uin,
            "skey": skey,
            "p_skey": p_skey,
            "zzpaneluin": self.uin,
            "p_uin": self.uin,
            "qzonetoken": "",
            "zzpanelkey": "",
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "refer": "shuoshuo",
            "output_type": "json",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": (
                "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
            ),
            "url": f"{UPLOAD_URL}?g_tk={bkn}",
            "base64": "1",
            "picfile": base64.b64encode(raw).decode("ascii"),
            "qzreferrer": referrer,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": "https://user.qzone.qq.com",
            "Referer": referrer,
            "X-Requested-With": "XMLHttpRequest",
        }
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            async with session.post(
                UPLOAD_URL,
                params={"g_tk": str(bkn)},
                data=data,
                cookies=cookies,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()

        body = _strip_callback(text)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise QzoneError(f"图片上传返回看不懂：{text[:120]}") from exc

        code = payload.get("code", payload.get("ret"))
        if str(code) not in {"0"}:
            raise QzoneError(
                f"图片上传失败：{payload.get('message') or payload.get('msg') or code}"
            )
        richval = self._build_richval(payload)
        if not richval:
            raise QzoneError(f"上传成功但字段不全：{body[:160]}")
        log.info("图片已上传，richval 长度 %d", len(richval))
        return {"richval": richval, "url": str(Qzone._find(payload, ("url",)) or "")}

    async def publish(
        self,
        content: str,
        private: int | None = None,
        richval: str = "",
    ) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise QzoneError("内容为空")
        if len(content) > 1000:
            content = content[:1000]

        cookies, bkn = await self._cookies()
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            token = await self._qzonetoken(session, cookies)
            form = {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "conformat": "1",
                # 纯文字时 richtype/richval 必须留空；带图时 richtype=1 且 richval 来自 upload_image
                "richtype": "1" if richval else "",
                "richval": richval,
                "private": str(self.private if private is None else private),
                "lbsinfo": "",
                "g_tk": str(bkn),
                "qzonetoken": token,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": "1",
                "to_sign": "0",
                "hostuin": self.uin,
                "code_version": "1",
                "format": "json",
                # 注意是 con，不是 content：用 content 时服务端会报 -10005「您未输入内容」
                "con": content,
            }
            async with session.post(
                f"{PUBLISH_URL}?g_tk={bkn}",
                data=form,
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()

        result = _parse_result(text)
        result["content"] = content
        return result

    async def comments(self, tid: str, *, limit: int = 50) -> dict[str, Any]:
        """读一条说说下面的评论（只读，用来回评论）。

        走网页版 `emotion_cgi_msgdetail_v6`：和发说说同一套 cookie + g_tk。
        返回可能是 JSON，也可能是 `_preloadCallback({...})`，两种都兜住；
        认不出就当没有评论（`ok=False`），不抛 —— 一条旧说说不能把回评论循环打断。
        """
        tid = str(tid or "").strip()
        if not tid:
            return {"ok": False, "code": None, "items": [], "content": "", "raw": "no tid"}
        cookies, bkn = await self._cookies()
        params = {
            "uin": self.uin,
            "tid": tid,
            "hostuin": self.uin,
            "not_trunc_con": "1",
            "code_version": "1",
            "format": "json",
            "g_tk": str(bkn),
        }
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
                async with session.get(
                    DETAIL_URL,
                    params=params,
                    cookies=cookies,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    text = await resp.text()
        except Exception as exc:  # noqa: BLE001 - 网络抖动当这次没读到
            log.warning("读评论失败 tid=%s: %s", tid, type(exc).__name__)
            return {"ok": False, "code": None, "items": [], "content": "", "raw": str(exc)[:200]}

        body = _strip_callback(text)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("读评论返回看不懂 tid=%s: %s", tid, text[:120])
            return {"ok": False, "code": None, "items": [], "content": "", "raw": text[:200]}
        if not isinstance(data, dict):
            return {"ok": False, "code": None, "items": [], "content": "", "raw": text[:200]}

        raw_items = data.get("commentlist") or data.get("commentList") or []
        items = _parse_comments(raw_items)
        return {
            "ok": bool(items) or data.get("code") == 0 or "commentlist" in data,
            "code": data.get("code"),
            "items": items[:limit],
            "content": _clean_comment(str(data.get("content") or "")),
            "raw": "",
        }

    async def reply(
        self,
        tid: str,
        content: str,
        *,
        name: str = "",
        host_uin: str | None = None,
    ) -> dict[str, Any]:
        """在某条说说下面评论一句（回别人的评论）。

        实测（2026-10-27）：`emotion_cgi_re_feeds` + `topicId={宿主}_{tid}__1`
        + `format=json` 能发出去，返回 `{"code":0,...}`。`emotion_cgi_reply_v6`
        那套老参数没试通，别再混着用。
        嵌套回复（把回复挂在某条评论下面）这条接口不认，发出来还是平铺评论；
        `paramstr="@昵称 正文"` 只负责让对方收到提醒。
        """
        content = (content or "").strip()
        if not content:
            raise QzoneError("回复内容为空")
        tid = str(tid or "").strip()
        if not tid:
            raise QzoneError("没给 tid")

        host = str(host_uin or self.uin)
        cookies, bkn = await self._cookies()
        form = {
            "topicId": f"{host}_{tid}__1",
            "uin": self.uin,
            "hostUin": host,
            "content": content,
            "format": "json",
            "plat": "qzone",
            "source": "ic",
            "platformid": "52",
            "ref": "feeds",
        }
        if name:
            form["paramstr"] = f"@{name} {content}"
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            async with session.post(
                f"{COMMENT_URL}?g_tk={bkn}",
                data=form,
                cookies=cookies,
                headers={
                    "Referer": f"https://user.qzone.qq.com/{host}",
                    "Origin": "https://user.qzone.qq.com",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()

        result = _parse_result(text)
        result["tid"] = tid
        result["content"] = content
        return result

