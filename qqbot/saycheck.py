"""发出去之前自己再过一遍：像不像套话、像不像刚刚已经说过一遍。

和 `antislop` 的分工（两个都是纯本地、0 token）：

| 模块 | 管什么 | 命中后 |
|---|---|---|
| `antislop` | **固定套话**：这条我认、本鱼记下了、认了认了 | 带着提示重说一次，还带就删掉那一小句 |
| `saycheck` | **软的两类**：作文腔叠在一起、跟自己刚才说的话太像 | 带着提示重说一次；不删句子 |

为什么不合并成一个词表：固定套话是硬证据，命中就该改；作文腔单独一句
（「不是不行，而是太贵」）是正常中文，只有**两处叠在一起**才像 AI 在写作文。
混在一起会把正常说话也判成八股（`antislop` 的教训：误伤比漏判更烦）。

同理，`本鱼` / 动作括号 / 「主人」这些是**人设**，不是八股 —— 这里一条都不碰。
所以这个模块只在群里跑（见 `app.py` 的 `reply_ctx["saycheck"]`），私聊照人设说。

命中只代表「值得重说一次」，不是禁词：重说还不过，就按原样发，不硬改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 跟自己以前说过的话比：归一化后字二元组的 Dice 系数
REPEAT_THRESHOLD = 0.82
# 太短的话不算重复（「好」「好嘛」「行」在同一个群里出现两次很正常）
REPEAT_MIN_CHARS = 8
# 复读提示里引用原句最多几个字
_QUOTE_LIMIT = 18

# 作文腔规则：(代号, 正则, 给人看的说法, 记几分)
# 强信号记 2 分（单独出现就够），弱信号记 1 分（要两条叠在一起）。
_RULES: tuple[tuple[str, re.Pattern[str], str, int], ...] = (
    (
        "summary",
        re.compile(r"^(?:总之|所以说|由此可见|总的来说|说到底)[，,：:]?"),
        "一开口就总结（总之 / 所以说）",
        2,
    ),
    (
        "report",
        re.compile(r"值得一提|希望对你有(?:所)?帮助|让我们一起|综上"),
        "报告腔、客服腔",
        2,
    ),
    (
        "not-but",
        re.compile(r"不是.{1,12}[，,]\s*(?:而)?是"),
        "「不是……而是……」式对仗",
        1,
    ),
    (
        "contrast",
        re.compile(r"不仅.{0,12}而且|与其.{0,12}不如|看似.{0,12}实则"),
        "工整的对仗句",
        1,
    ),
    (
        "essence",
        re.compile(r"归根结底|从本质上(?:讲|说)"),
        "讲道理腔（归根结底 / 本质上）",
        1,
    ),
    (
        "stepwise",
        re.compile(r"^(?:首先|其次|最后)[，,]"),
        "分点讲（首先 / 其次 / 最后）",
        1,
    ),
)

# 叠到几分才算「像 AI 在写作文」
_FLAG_POINTS = 2

_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def _norm(text: str) -> str:
    """比对用的归一化：去掉标点/空白/表情，只留汉字字母数字。"""
    return _NORM_RE.sub("", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def dice(a: str, b: str) -> float:
    """两个字符串的字二元组 Dice 系数（中文短句比编辑距离稳）。"""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if len(x) < 2 or len(y) < 2:
        return 0.0
    gx, gy = _bigrams(x), _bigrams(y)
    common = len(gx & gy)
    return 2 * common / (len(gx) + len(gy))


@dataclass(frozen=True)
class Result:
    """一次检查的结果。`problems` 为空就是可以发。"""

    problems: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    repeat_score: float = 0.0
    repeat_line: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def find_soft(text: str) -> list[tuple[str, str, int]]:
    """返回命中的作文腔规则：[(代号, 说法, 分数), ...]（保持规则顺序）。"""
    body = str(text or "")
    out: list[tuple[str, str, int]] = []
    for code, pattern, why, points in _RULES:
        if pattern.search(body):
            out.append((code, why, points))
    return out


def similar_to_recent(
    text: str,
    recent: list[str] | tuple[str, ...] | None,
    *,
    threshold: float = REPEAT_THRESHOLD,
    min_chars: int = REPEAT_MIN_CHARS,
) -> tuple[str, float]:
    """跟最近说过的话比，返回最像的一条 (原句, 相似度)；没有就 ("", 0)。"""
    if not recent or len(_norm(text)) < min_chars:
        return "", 0.0
    best_line, best_score = "", 0.0
    for line in recent:
        if not isinstance(line, str) or len(_norm(line)) < min_chars:
            continue
        score = dice(text, line)
        if score > best_score:
            best_line, best_score = line, score
    if best_score < threshold:
        return "", 0.0
    return best_line, best_score


def check(
    text: str,
    recent: list[str] | tuple[str, ...] | None = None,
) -> Result:
    """发之前看一眼。`recent` 是同一会话里她自己最近说过的话（旧的在前新的在后都行）。

    纯函数：不读文件、不看配置、不调模型；调用方决定跑不跑（群聊 / 开关）。
    """
    if not str(text or "").strip():
        return Result()

    problems: list[str] = []
    codes: list[str] = []

    soft = find_soft(text)
    if sum(points for _, _, points in soft) >= _FLAG_POINTS:
        for code, why, _ in soft:
            codes.append(f"soft:{code}")
            problems.append(why)

    line, score = similar_to_recent(text, recent)
    if line:
        codes.append("repeat:self")
        quote = line if len(line) <= _QUOTE_LIMIT else line[:_QUOTE_LIMIT] + "…"
        problems.append(f"跟你刚才说的太像（{round(score * 100)}%）：{quote}")

    return Result(
        problems=tuple(problems),
        codes=tuple(codes),
        repeat_score=score,
        repeat_line=line,
    )


def retry_note(problems: tuple[str, ...] | list[str]) -> str:
    """重说时给模型看的一句提示：说清问题，不解释、不跳出人设。"""
    head = str(problems[0]) if problems else "有点套话"
    if len(head) > 60:
        head = head[:60]
    return (
        f"（刚才那句：{head}。重说一句，用你平常的口气；"
        "别用固定句式，也别解释。）"
    )
