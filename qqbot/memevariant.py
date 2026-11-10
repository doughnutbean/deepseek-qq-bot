"""梗的变体写法：缩写、数字谐音、同音字 —— 同一条梗的不同写法。

统计只认「同一个串」：群里发 `f8fq`，梗表/梗百科里记的是 `飞八分钱`。
不归一就会出现两种坏结果：

- 她不知道 `f8fq` 就是那个骂人梗，**跟着复读**（`risk_of()` 漏判）；
- 梗表把 `f8fq` 当成新梗收一条，主人还得再标一次。

归一不改数据，只做「读」：`skeleton()` 把任意写法压成拼音首字母骨架 ——
汉字取首字母，数字按读音取：8→b（ba）、2→e（er）、1→y（yi）。
所以 `飞八分钱` / `肥八分钱` / `fbfq` / `f8fq` 的骨架都是 `fbfq`；
`永远的神` / `yyds` 都是 `yyds`；`牛逼` / `nb` 都是 `nb`。

两条边界：

- **骨架只用来「认成同一个词」，不用来判风险**：是不是骂人/黄腔/刷屏还是
  `memetone` 按词条内容判，不然光看字母会把正常缩写误伤；
- 骨架认不出来的（换字了但不换音，如「鱼八分钱」），走 `SpellBook` 显式链接：
  主人一句「写法：鱼八分钱 = 飞八分钱」，或群里有人解释时自动学。
  显式链接存 `data/meme_aliases.json`，跟梗表分开（一条梗可能只在梗百科里）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import pinyin_table

log = logging.getLogger(__name__)

MIN_CODE = 2
MAX_CODE = 12
MAX_TERM = 12
MAX_LINKS = 500

# 数字按读音取首字母：0 ling、1 yi、2 er、3 san、4 si、5 wu、6 liu、7 qi、8 ba、9 jiu
_DIGITS = {
    "0": "l",
    "1": "y",
    "2": "e",
    "3": "s",
    "4": "s",
    "5": "w",
    "6": "l",
    "7": "q",
    "8": "b",
    "9": "j",
}
_VOWELS = frozenset("aeiou")
_CODE_RE = re.compile(r"(?<![a-z0-9])[a-z0-9]{%d,%d}(?![a-z0-9])" % (MIN_CODE, MAX_CODE))
# URL / 文件名 / @某人 里的串不是缩写
_NEIGHBOR = {":", ".", "/", "\\", "@", "_", "-"}

# 群里解释缩写：「f8fq 就是飞八分钱」「飞八分钱也叫 f8fq」「yyds＝永远的神」
EXPLAIN_PATTERNS = (
    re.compile(
        r"(?P<code>[A-Za-z0-9]{2,12})\s*(?:就是|即|＝|=|→|->|是)\s*"
        r"(?P<term>[^\s，。！？,.!?]{1,16})"
    ),
    re.compile(
        r"(?P<term>[^\s，。！？,.!?]{1,16})\s*"
        r"(?:也叫|又叫|简称|缩写为|缩写是|简写成|写成)\s*"
        r"(?P<code>[A-Za-z0-9]{2,12})"
    ),
)
_EXPLAIN_TAIL = (
    "的缩写",
    "的简称",
    "的简写",
    "的谐音",
    "缩写",
    "简称",
    "简写",
    "谐音",
    "首字母",
)


def fold(text: Any) -> str:
    """全角转半角 + 小写：`ｆ８ｆｑ` 和 `f8fq` 是同一个写法。"""
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def clean(text: Any) -> str:
    """写法只留字母数字（去掉空格、引号、标点）。"""
    return re.sub(r"[^0-9a-z]", "", fold(text))


def clean_term(text: Any) -> str:
    """词条名：去掉空白和两头的标点，保留汉字。"""
    body = re.sub(r"\s+", "", str(text or ""))
    return body.strip("「」『』“”\"'（）()[]【】:：,，.。!！?？;；、")


def strip_explain_suffix(term: Any) -> str:
    """去掉解释里的尾巴：「飞八分钱的缩写」→「飞八分钱」。"""
    body = str(term or "")
    for _ in range(2):
        for tail in _EXPLAIN_TAIL:
            if body.endswith(tail) and len(body) > len(tail):
                body = body[: -len(tail)]
                break
        else:
            break
    return body


def skeleton(text: Any) -> str:
    """写法 → 拼音首字母骨架：飞八分钱 / fbfq / f8fq 都是 `fbfq`。"""
    out: list[str] = []
    for ch in fold(text):
        if "a" <= ch <= "z":
            out.append(ch)
        elif ch in _DIGITS:
            out.append(_DIGITS[ch])
        else:
            letter = pinyin_table.initial(ch)
            if letter:
                out.append(letter)
    return "".join(out)


def is_code(text: Any) -> bool:
    """这个串像不像「缩写/数字谐音」，不像就返回 False。

    只认 2~12 位字母数字、至少一个字母，而且满足一条：
    带数字（8→ba 那种写法），或者一个元音都没有（拼音首字母基本不长元音）。
    `666` 这种纯数字、`hello` 这种正常英文词都不算 —— 宁可漏，也别把普通聊天
    当成缩写去认。
    """
    body = clean(text)
    if not (MIN_CODE <= len(body) <= MAX_CODE):
        return False
    if not any("a" <= ch <= "z" for ch in body):
        return False
    if any(ch.isdigit() for ch in body):
        return True
    return not (set(body) & _VOWELS)


def codes_in(text: Any) -> list[str]:
    """一句话里像缩写的串（按出现顺序，去重，最多 8 个）。"""
    body = fold(text)
    out: list[str] = []
    for match in _CODE_RE.finditer(body):
        code = match.group(0)
        if not is_code(code):
            continue
        before = body[match.start() - 1 : match.start()]
        after = body[match.end() : match.end() + 1]
        if before in _NEIGHBOR or after in _NEIGHBOR:
            continue
        if code not in out:
            out.append(code)
    return out[:8]


class SpellBook:
    """写法 → 正词 的本地链接表（`data/meme_aliases.json`）。

    只记骨架推不出来的写法（「鱼八分钱」这种换字不换音的）；骨架能推出来的
    `f8fq` / `nb` 不落盘，免得表越攒越大。写入来源只有两个：
    主人「写法：X = Y」，和群里有人解释（「X 就是 Y」）且 Y 是已有词条。
    """

    NAME = "meme_spell"

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._store = self._load()

    # ---------- 持久化 ----------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "updated_at": 0.0, "links": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("not a dict")
        except (OSError, ValueError, TypeError) as exc:
            log.warning("写法链接表读取失败，按空表处理：%s", exc)
            return self._empty()
        base = self._empty()
        links = {}
        for key, item in (data.get("links") or {}).items():
            alias = clean_term(key)
            if not alias or not isinstance(item, dict):
                continue
            term = clean_term(item.get("to"))
            if not term or term == alias:
                continue
            links[alias] = {
                "to": term,
                "source": str(item.get("source") or "owner")[:10],
                "at": float(item.get("at") or 0.0),
            }
        if links:
            base["links"] = links
        base["updated_at"] = float(data.get("updated_at") or 0.0)
        return base

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._store["updated_at"] = time.time()
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._store, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("写法链接表写入失败：%s", exc)

    # ---------- 读 ----------

    def links(self) -> dict[str, str]:
        """{写法: 正词}，给排查/测试用。"""
        return {
            key: str(item.get("to") or "")
            for key, item in (self._store.get("links") or {}).items()
            if isinstance(item, dict) and item.get("to")
        }

    @staticmethod
    def _key(alias: Any) -> str:
        """写法的键：去空白/标点，保留汉字（换字写法也是写法）。"""
        return clean_term(alias)

    def resolve(self, alias: Any) -> str:
        """这个写法显式链接到哪个正词；没有返回空串。

        整句也能用：句子里某个缩写有链接就算（「f8fq 真得发」）。
        """
        if not self.enabled:
            return ""
        links = self._store.get("links") or {}
        item = links.get(self._key(alias))
        if isinstance(item, dict) and item.get("to"):
            return str(item["to"])
        for code in codes_in(alias):
            item = links.get(code)
            if isinstance(item, dict) and item.get("to"):
                return str(item["to"])
        return ""

    def of(self, term: Any) -> list[str]:
        """这条词有哪些显式写法；按写入时间倒序。"""
        wanted = clean_term(term)
        rows = [
            (str(item.get("at") or 0.0), key)
            for key, item in (self._store.get("links") or {}).items()
            if isinstance(item, dict) and item.get("to") == wanted
        ]
        rows.sort(reverse=True)
        return [key for _at, key in rows]

    # ---------- 写 ----------

    def link(
        self,
        alias: Any,
        term: Any,
        *,
        source: str = "owner",
        now: float | None = None,
    ) -> bool:
        """把「写法」挂到「正词」上。写法本身可以是汉字（换字不换音）。"""
        if not self.enabled:
            return False
        key = self._key(alias)
        to = clean_term(term)
        if not key or not to or key == to:
            return False
        if len(key) > MAX_CODE or len(to) > MAX_TERM:
            return False
        if to.isascii() and not any("a" <= ch <= "z" for ch in to):
            return False  # 正词不能是纯数字
        links = self._store.setdefault("links", {})
        old = links.get(key)
        if isinstance(old, dict) and old.get("to") == to and old.get("source") == source:
            return False  # 没变化，不写盘
        links[key] = {
            "to": to,
            "source": str(source or "owner")[:10],
            "at": time.time() if now is None else float(now),
        }
        # 封顶：超了先丢最老的自动链接，主人标的不动
        if len(links) > MAX_LINKS:
            keep = sorted(
                links.items(),
                key=lambda kv: (
                    1 if str(kv[1].get("source")) == "owner" else 0,
                    float(kv[1].get("at") or 0.0),
                ),
                reverse=True,
            )[:MAX_LINKS]
            self._store["links"] = dict(keep)
        self._save()
        return True

    def unlink(self, alias: Any) -> bool:
        if not self.enabled:
            return False
        key = self._key(alias)
        links = self._store.get("links") or {}
        if key not in links:
            return False
        links.pop(key, None)
        self._save()
        return True

    def status(self) -> dict[str, Any]:
        links = self._store.get("links") or {}
        return {
            "enabled": self.enabled,
            "count": len(links),
            "owner": sum(
                1
                for item in links.values()
                if isinstance(item, dict) and item.get("source") == "owner"
            ),
            "updated_at": self._store.get("updated_at", 0.0),
        }
