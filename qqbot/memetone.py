"""梗的「能懂」和「能用」分开：骂人、黄腔、刷屏梗只认不用。

词库里的 `usage` 是写给「知道这个梗的人」看的，不是写给「要接这个梗的人」看的。
比如「飞八分钱」原词条明说它是拿主播家人开骂的谐音黑话，但前一句写着
「可在任何提及张顺飞的场合刷屏使用」——直接喂给模型，她就会跟着喊。

这里只做三件事：

- `risk()`：给一段梗资料判风险（骂人 / 黄腔 / 刷屏），判不出返回空串；
- `question_note()` / `mention_note()`：问梗、平时提到时，给「懂但不用」的提示，
  不复述原句、不贴粗口模板；
- `line_note()`：群梗表里给这类条目标一行「只认不用」。

判断只认比较硬的词，不把「调侃 / 嘲讽」这种语气标签一律当风险：那样会把
「老六」「帅不帅」这类正常玩笑也误伤。真正的贬称靠 `means`（蛆 / 骚货 / 肛门）
和词条里的「人身攻击 / 恶俗粗口 / 刷屏」这类明说。
"""

from __future__ import annotations

import re
from typing import Any

ABUSE = "abuse"
SEXUAL = "sexual"
SPAM = "spam"
RESTRICTED = "restricted"

LABELS = {ABUSE: "骂人", SEXUAL: "黄腔", SPAM: "刷屏", RESTRICTED: "只认不用"}

# 「骂人」：只认明说 + 明确的贬称。不把「嘲讽」「贬义」单独算 ——
# 那只是语气，很多正常玩笑也带这个标签。
_ABUSE_RE = re.compile(
    r"人身攻击|辱骂|恶俗|粗口|诅咒|死妈|你妈死|死全家|族谱|网暴|开盒|挂人|"
    r"地域黑|歧视|侮辱|傻逼|脑残|智障|废物|狗东西|贱人|婊子|母狗|蛆"
)
# 「黄腔」：同样只认清点的词。不要用单字「骚 / 阴 / 乳」，否则「阴人」「乳白」
# 这种正常说法也会被误判。
_SEXUAL_RE = re.compile(
    r"黄腔|色情|性骚扰|性暗示|约炮|裸聊|裸照|口交|肛交|做爱|嫖娼|"
    r"骚货|淫|阴茎|阴道|肛门|性器官|性行为|乳交|乳照"
)
_SPAM_RE = re.compile(r"刷屏|连击|弹幕轰炸|复读轰炸")
# 词条自己就写着「知道意思就行 / 不用接」：不用再判脏话，直接按只认不用。
_RESTRICT_RE = re.compile(
    r"不用接|不用主动|别主动用|知道意思就行|只认不用|不用跟着|不是给你用"
)


def risk(*parts: Any) -> str:
    """给梗的标题/场景/用法/means/接法判风险；没有风险返回空串。"""
    blob = " ".join(str(part or "") for part in parts)
    if not blob.strip():
        return ""
    low = blob.lower()
    if _SEXUAL_RE.search(low):
        return SEXUAL
    if _ABUSE_RE.search(low):
        return ABUSE
    if _SPAM_RE.search(low):
        return SPAM
    if _RESTRICT_RE.search(low):
        return RESTRICTED
    return ""


def label(kind: str) -> str:
    """给日志/提示用的中文短名。"""
    return LABELS.get(kind, "")


def question_note(title: str, kind: str) -> str:
    """有人明确问「X 是什么梗」时：说清是哪一类，但不复述原句。"""
    name = str(title or "").strip() or "这个词"
    if kind == SEXUAL:
        return (
            f"（网上的说法：「{name}」是黄腔谐音，本鱼知道意思，但不复述、不跟着用；"
            "有人问就说不方便细说。）"
        )
    if kind == SPAM:
        return (
            f"（网上的说法：「{name}」是刷屏用的梗，本鱼知道，但不跟着刷、不复述。）"
        )
    if kind == RESTRICTED:
        return f"（网上的说法：「{name}」知道意思就行，不用接、别主动用。）"
    return (
        f"（网上的说法：「{name}」是拿人格/家人开骂的梗，本鱼知道是骂人的就行；"
        "不复述原句、不跟着喊，也不对真人用。）"
    )


def mention_note(title: str, kind: str) -> str:
    """平时聊天里提到时：只提醒「知道就行」，不教她接。"""
    name = str(title or "").strip() or "这个词"
    if kind == SEXUAL:
        return f"（「{name}」是黄腔梗，知道意思就行；别主动用、别跟着说。）"
    if kind == SPAM:
        return f"（「{name}」是刷屏梗，知道就行；别跟着刷。）"
    if kind == RESTRICTED:
        return f"（「{name}」知道意思就行；别主动用、不用接。）"
    return f"（「{name}」是骂人梗，知道就行；别主动用、别跟着复读，也别对真人喊。）"


def line_note(text: str, kind: str, scene: str = "") -> str:
    """群梗表里的一条：给「只认不用」的标签，只用安全的说法带一句。"""
    name = str(text or "").strip()
    if kind == RESTRICTED:
        return f"· 「{name}」（知道就行，不用接）"
    if kind == ABUSE:
        tag = "贬称" if "贬称" in str(scene or "") else "骂人梗"
        return f"· 「{name}」（{tag}，只认不用）"
    if kind == SEXUAL:
        return f"· 「{name}」（黄腔谐音，只认不用）"
    if kind == SPAM:
        return f"· 「{name}」（刷屏梗，只认不用）"
    return f"· 「{name}」（只认不用）"
