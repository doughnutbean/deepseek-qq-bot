"""大肥鱼对自己的认知：这一版会什么、哪些开着、什么时候学会的。

**唯一事实源**。改这里会同时影响：

- system prompt 里的「你这一版会什么」块（`app.Bot._system_prompt`）
- 主人发「更新」看到的清单（`app.Bot._handle_owner_command`）
- 主人发「指令」里的功能开关行（`app.Bot._commands_text`）
- 「状态」里的功能行（`app.Bot._status_reply`）
- `docs/能力表.md`（由 `tools/check_capabilities.py --write` 生成）

三条硬约束：

1. prompt 块里**不许出现 `YYYY-MM-DD` 和「现在是」**。前缀缓存要求 system
   字节稳定（见 `tools/test_prompt_cache.py`）；「最近学会」按进程启动那天算一次，
   重启才更新 —— 这和「改完要重启才生效」是同一件事。
2. **只把开着的功能说成会**；关掉的单独列一行，防止她答应做不到的事。
3. 新功能上线时在这里加一条。`tools/check_capabilities.py` 会校验开关字段、
   「指令」里的关/开说法真的能被 `commands.parse()` 认出来、文档和表一致。
4. **这里只写她会做什么，不写人设、称呼、关系、真名。** `persona.md` 的修改
   留在 persona.md，不进能力表、也不进 git log 草稿 —— 否则她哪天照着说出来就出戏了。
5. 手写条目在 `MANUAL_CAPABILITIES`；`Capability:` commit trailer 自动登记的条目
   在 `capabilities_auto.json`，由 `tools/capability_sync.py` 生成，别手改。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 「最近学会」的窗口和 prompt 里最多列几条（窗口内也只取最新的几条，免得块太长）
RECENT_DAYS = 7
MAX_RECENT_IN_PROMPT = 5

# 她真的做不到的事。写进 prompt 和「更新」清单，防止被问「你会不会」时吹牛。
# 只放**能力边界**，不放性格/说话方式（那些在 persona.md）。
LIMITS: tuple[str, ...] = (
    "发不了网上的图片（搜到了也发不出去）",
    "群里改不了签名、头像、昵称（只有主人私聊时才行）",
    "加不了好友；别人拉你进群，你得先问清情况、等主人点头才能进",
)


@dataclass(frozen=True)
class Capability:
    """一条她会的能力。

    key      唯一标识；可开关的功能用 prefs 里的 key（forward / hot_trends …）
    skill    她自己的口吻，一句话说会什么（会直接进 prompt）
    since    学会/上线日期 YYYY-MM-DD
    flag     Config 里的开关字段；空串 = 一直有
    label    中文名（回执、「指令」清单、文档用）
    short    「状态」里的短名（默认用 label）
    off/on   「指令」里的关闭/打开说法；必须能被 commands.parse() 认出来
    note     「指令」清单里的备注
    in_status 是否出现在「状态」的功能行
    in_prompt 是否进 system prompt（主人专属/内部能力设 False，避免对非主人许愿）
    audience all / owner（owner = 不进群聊 prompt，避免对群友许愿）
    """

    key: str
    skill: str
    since: str
    flag: str = ""
    label: str = ""
    short: str = ""
    off: str = ""
    on: str = ""
    note: str = ""
    in_status: bool = True
    in_prompt: bool = True
    audience: str = "all"


# 顺序有讲究：「指令」清单和「状态」功能行都按这个顺序出。
MANUAL_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="memory",
        skill="聊过的事、你说过的喜好，你能记住",
        since="2026-10-22",
        flag="enable_memory",
        label="记忆",
        short="",
        in_status=False,
    ),
    Capability(
        key="search",
        skill="外面的新闻、价格、版本，你能先搜再答",
        since="2026-10-22",
        flag="enable_search",
        label="联网搜索",
        short="搜索",
    ),
    Capability(
        key="vision",
        skill="别人发的图、动图，你能看懂；动图会抽几帧拼起来看动作",
        since="2026-10-22",
        flag="enable_vision",
        label="看图",
    ),
    Capability(
        key="sticker",
        skill="你能看情境挑表情包，也能按图里的字/画面找出来发",
        since="2026-10-22",
        flag="enable_sticker",
        label="表情包",
        in_status=False,
    ),
    Capability(
        key="sticker_steal",
        skill="群里别人发的图，主角是你，你会收进自己的表情包库",
        since="2026-10-26",
        flag="enable_sticker_steal",
        label="捡表情包",
        in_status=False,
    ),
    Capability(
        key="sticker_replace",
        skill="群友能帮你把有水印的表情包换成干净图，你会先核对是不是同一张",
        since="2026-10-27",
        flag="enable_sticker_replace",
        label="换表情包",
        in_status=False,
    ),
    Capability(
        key="bili",
        skill="有人发 B 站链接，你能说出是哪个视频",
        since="2026-10-23",
        flag="enable_bili_card",
        label="B站卡片",
        short="B站卡",
    ),
    Capability(
        key="bili_live",
        skill="盯 B 站直播间时，你能看标题、弹幕和最近几帧画面；高频弹幕会攒成候选",
        since="2026-10-27",
        flag="enable_bili_live",
        label="直播陪看",
        short="直播",
        note=(
            "能看标题、弹幕和最近几帧画面（后台一直在抓最近 2 分钟，没有声音）；"
            "别答「只看得到弹幕」或「没有画面功能」。多群可同看一个房间，重启会接着盯。"
            "「看直播 12345」开始，「直播梗」看候选"
        ),
    ),
    Capability(
        key="forward",
        skill="别人转发的聊天记录，你能读到里面的话",
        since="2026-10-23",
        flag="enable_forward",
        label="合并转发",
        short="转发",
        off="关闭合并转发",
        on="打开合并转发",
        note="要不要读别人转发的聊天记录",
    ),
    Capability(
        key="hot_trends",
        skill="每天的热搜和新梗，你有一份小抄",
        since="2026-10-23",
        flag="enable_hot_trends",
        label="热梗知识库",
        short="热梗",
        off="关闭热梗",
        on="打开热梗",
        note="每天的热搜/新梗小抄",
    ),
    Capability(
        key="memes",
        skill="这个群里的梗，你能听懂、也能接",
        since="2026-10-26",
        flag="enable_memes",
        label="群里的梗",
        short="群梗",
        off="关闭群梗",
        on="打开群梗",
        note="群里反复说的梗，你有一张小抄",
    ),
    Capability(
        key="meme_wiki",
        skill="网上常见的梗，你听得懂；安全的按用法接，骂人/黄腔的只认不用",
        since="2026-10-27",
        flag="enable_meme_wiki",
        label="梗百科",
        short="梗百科",
        note="问梗给意思和用法；骂人/黄腔/刷屏的只认不用，平时提到也只提醒别乱用",
        in_status=False,
    ),
    Capability(
        key="meme_candidates",
        skill="聊天里搜到的梗先收成候选，你确认了才进梗表",
        since="2026-10-28",
        label="候选梗",
        short="候选",
        note="搜索候选不注入；确认梗：X 才进表",
        in_status=False,
        audience="owner",
    ),
    Capability(
        key="meme_intake",
        skill="想记一个梗，发「记梗 X」先查清楚，再由你拍板入不入库",
        since="2026-10-27",
        label="记梗前查",
        short="查梗",
        note="先搜一次做判断，结果私聊给主人；入库/不入库由主人决定",
        in_status=False,
        audience="owner",
    ),
    Capability(
        key="homophone_list",
        skill="群里把哪个词当成谐音代称，你说一句就能列出来",
        since="2026-10-27",
        label="看谐音",
        short="谐音",
        note="只读，0 token；群里看本群+通用，私聊看全部",
        in_status=False,
        audience="owner",
    ),
    Capability(
        key="repeat_along",
        skill="群里很多人复读一句，安全的话你也会跟着接一句",
        since="2026-10-27",
        flag="enable_repeat_along",
        label="跟着复读",
        short="复读",
        off="关闭跟着复读",
        on="打开跟着复读",
        note="同一句被多人短时间复读时，跟着发一次；骂人/黄腔/刷屏的不跟",
    ),
    Capability(
        key="group_style",
        skill="这个群说话的习惯，你能学着点",
        since="2026-10-23",
        flag="enable_group_style",
        label="群风格统计",
        short="群风格",
        off="关闭群风格",
        on="打开群风格",
        note="学这个群怎么说话",
    ),
    Capability(
        key="saycheck",
        skill="群里说话之前，你会自己再过一遍：像作文、像刚说过的话，就重说一句",
        since="2026-10-29",
        flag="enable_saycheck",
        label="说人话",
        short="人话",
        off="关闭说人话",
        on="打开说人话",
        note="只在群里查：作文腔、重复自己；命中才重说一次，私聊不管",
    ),
    Capability(
        key="group_log",
        skill="群里你没接的话，你也能旁听到",
        since="2026-10-26",
        flag="enable_group_log",
        label="群旁听",
        in_status=False,
    ),
    Capability(
        key="corpus_recall",
        skill="群里以前聊过的事，你能当背景想起来",
        since="2026-10-26",
        flag="enable_corpus_recall",
        label="群语料理解",
        short="旧事",
        note="从本群历史里找相关旧消息，只当背景，不改说话方式",
        in_status=False,
    ),
    Capability(
        key="mood",
        skill="同一件事被问烦了，你会闹别扭",
        since="2026-10-25",
        flag="enable_mood",
        label="生气的设定",
        short="生气",
        off="关闭生气的设定",
        on="打开生气的设定",
        note="同一件事问烦了她会闹",
    ),
    Capability(
        key="auto_ban",
        skill="广告和骚扰，你能自己认出来并拉黑",
        since="2026-10-25",
        flag="enable_auto_ban",
        label="自动拉黑",
        off="关闭自动拉黑",
        on="打开自动拉黑",
        note="广告/骚扰/冒充主人/色情图，先警告三次",
    ),
    Capability(
        key="at",
        skill="有人 @ 你，你会点回去；一群人同时在说时，你用 @ 说明这句是接谁的",
        since="2026-10-26",
        flag="enable_at",
        label="点名@人",
        short="点名",
        off="别@人了",
        on="可以@人了",
        note="默认关；插话不点，回话和人多时点，不数次数",
    ),
    Capability(
        key="friend_accept",
        skill="好友申请，你能按熟悉度、最近有没有聊过、好感来决定通不通过",
        since="2026-10-25",
        flag="enable_friend_accept",
        label="好友申请",
        off="关闭好友申请",
        on="打开好友申请",
        note="只批「聊过」以上、最近 30 天有互动、好感不为负的人；一天最多几个",
    ),
    Capability(
        key="qzone",
        skill="主人让你发说说时，你能发到 QQ 空间",
        since="2026-10-22",
        flag="enable_qzone",
        label="QQ空间",
        short="空间",
    ),
    Capability(
        key="roster",
        skill="群里谁是群主、管理员、普通群友，你能分清",
        since="2026-10-26",
        label="群名册",
        in_status=False,
    ),
    Capability(
        key="group_access",
        skill="在哪个群说话、哪个群不理，主人说一句就能加进来或请出去",
        since="2026-10-27",
        label="群白名单",
        short="群名单",
        note="不在白名单的群，你一句话都不回",
        in_status=False,
    ),
    Capability(
        key="concession",
        skill="群里有人要你改口，你会先嘴硬半句再慢慢让一步",
        since="2026-10-26",
        flag="enable_concession",
        label="改口游戏",
        in_status=False,
    ),
    Capability(
        key="favorability",
        skill="群友问好感度，你能分开报熟悉度（聊了多久）和好感（最近怎么相处）",
        since="2026-10-26",
        label="群友好感度",
        in_status=False,
    ),
    Capability(
        key="affection",
        skill="跟人处久了你会记住谁熟、谁让你不舒服；好感会随相处变，不只看聊了几句",
        since="2026-10-26",
        label="好感会变",
        in_status=False,
    ),
    Capability(
        key="member_rights",
        skill="群友能让你别接他的话、看/删关于自己的记忆；熟悉度够、好感不为负时能改称呼、关本群推送",
        since="2026-10-26",
        label="群友权限",
        in_status=False,
    ),
    Capability(
        key="proactive",
        skill="群里没人叫你，你也会挑着接话",
        since="2026-10-26",
        flag="enable_proactive",
        label="群聊主动插话",
        short="主动",
        in_status=False,
    ),
    Capability(
        key="greeting",
        skill="每次上线，你会先在群里说一句",
        since="2026-10-25",
        flag="enable_startup_greeting",
        label="上线问候",
        in_status=False,
    ),
    Capability(
        key="image_cache",
        skill="同一张图看过一次，你就记住了，下次不用再看",
        since="2026-10-25",
        flag="enable_image_cache",
        label="图片记忆",
        in_status=False,
    ),
    Capability(
        key="self_profile",
        skill="主人让你改的时候，你能改自己的签名、头像、昵称",
        since="2026-10-22",
        label="改自己资料",
        note="只在主人私聊里",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    Capability(
        key="proactive_dm",
        skill="主人冷落你时，你会主动私聊找他",
        since="2026-10-26",
        flag="enable_proactive_dm",
        label="主动私聊",
        note="只在主人私聊里",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    Capability(
        key="daily_report",
        skill="每天中午你会给主人报一次今天的账",
        since="2026-10-24",
        flag="enable_daily_report",
        label="每日账本",
        note="只发给主人",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    Capability(
        key="qzone_auto",
        skill="你自己想起来时，也会发一条说说",
        since="2026-10-25",
        flag="enable_qzone_auto",
        label="自动说说",
        note="主人专属，不接群友的要求",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    Capability(
        key="qzone_reply",
        skill="有人在你发的说说下面评论，你会自己回一句",
        since="2026-10-27",
        flag="enable_qzone_reply",
        label="回评论",
        short="回评",
        note="只看自己说说下面的评论；一天几条、半夜不回",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    # 2026-10-27：主人私聊里，她心里带着自己的账（今天花销 + 账户余额）。
    # 只对主人可见；不主动报数，所以不进「状态」功能行。动态上下文每轮已经把
    # 数字和「怎么用」带上了，这里不再占 system prompt 的「最近学的」名额。
    Capability(
        key="money_awareness",
        skill="你自己今天花了多少、账户里还剩多少，心里有数",
        since="2026-10-27",
        flag="money_awareness",
        label="自己的账",
        note="只在主人私聊里带数字；群友问还是现查本机账本，账户余额不往群里带",
        in_status=False,
        in_prompt=False,
        audience="owner",
    ),
    Capability(
        key="chris_lore",
        skill="有人问克里斯的口头禅、粉丝梗、笑话，你能从本地语料库翻出来",
        since="2026-10-28",
        label="克里斯语料",
        short="语料",
        note="本地 SQLite，0 token；只在问到克里斯/克区/克神时用",
        in_status=False,
    ),
)


# 自动登记的条目（`Capability:` commit trailer → tools/capability_sync.py）。
# 坏一条不能把机器人启动搞崩：跳过并记 warning，check_capabilities 会把它抓出来。
_AUTO_FILE = Path(__file__).with_name("capabilities_auto.json")


def _load_auto_capabilities() -> tuple[Capability, ...]:
    if not _AUTO_FILE.exists():
        return ()
    try:
        raw = json.loads(_AUTO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("capabilities_auto.json 读不了，按空表处理", exc_info=True)
        return ()
    if not isinstance(raw, list):
        log.warning("capabilities_auto.json 不是列表，按空表处理")
        return ()
    out: list[Capability] = []
    for item in raw:
        if not isinstance(item, dict):
            log.warning("capabilities_auto.json 有一条不是对象，已跳过：%r", item)
            continue
        try:
            out.append(Capability(**item))
        except TypeError:
            log.warning("capabilities_auto.json 有一条字段不对，已跳过：%r", item)
    return tuple(out)


CAPABILITIES: tuple[Capability, ...] = MANUAL_CAPABILITIES + _load_auto_capabilities()


# 主人能在聊天里开关的功能：key -> (Config 字段, 中文名)。app.py 直接用它。
FEATURE_ATTRS: dict[str, tuple[str, str]] = {
    cap.key: (cap.flag, cap.label) for cap in CAPABILITIES if cap.off and cap.on
}
HELP_FEATURES: tuple[Capability, ...] = tuple(
    cap for cap in CAPABILITIES if cap.off and cap.on
)
# 「状态」功能行的顺序：沿用原来的排列，别让一行状态突然换位置。
# 搜索/看图/B站卡/空间不能聊天开关，但状态里一直报，所以也在这。
_STATUS_ORDER = (
    "forward",
    "hot_trends",
    "group_style",
    "mood",
    "auto_ban",
    "at",
    "friend_accept",
    "search",
    "vision",
    "bili",
    "qzone",
)
_BY_KEY = {cap.key: cap for cap in CAPABILITIES}
STATUS_FEATURES: tuple[Capability, ...] = (
    tuple(_BY_KEY[key] for key in _STATUS_ORDER if key in _BY_KEY)
    # 自动登记的新开关接在已有顺序后面，不挤掉原来的位置
    + tuple(
        cap
        for cap in CAPABILITIES
        if cap.flag and cap.in_status and cap.key not in _STATUS_ORDER
    )
)


def since_of(capability: Capability) -> date:
    """解析 since；写错了直接抛 ValueError，测试/校验脚本会红。"""
    return datetime.strptime(capability.since, "%Y-%m-%d").date()


def is_enabled(capability: Capability, config: Any) -> bool:
    """没有开关 = 一直有；有开关就看当前 config（运行时被主人改过也算）。"""
    if not capability.flag:
        return True
    return bool(getattr(config, capability.flag, False))


def _visible(capability: Capability, is_group: bool) -> bool:
    return not (is_group and capability.audience == "owner")


def _recent_label(capability: Capability, today: date) -> str:
    """prompt 里的时间标签。只写 MM-DD 或「昨天/刚」，不写完整日期。"""
    days = (today - since_of(capability)).days
    if days <= 0:
        return "刚学会"
    if days == 1:
        return "昨天学会"
    return f"{since_of(capability).strftime('%m-%d')} 学会"


def _split_recent(
    config: Any, today: date, is_group: bool, *, for_prompt: bool = False
) -> tuple[list[Capability], list[Capability], list[Capability]]:
    """(最近学的, 其他会的, 现在关着的) —— 都只算当前开着的。

    for_prompt=True 时跳过 `in_prompt=False` 的主人专属能力；「更新」清单
    （for_prompt=False）仍然列出来，主人自己看得到。
    """
    enabled: list[Capability] = []
    disabled: list[Capability] = []
    for cap in CAPABILITIES:
        if not _visible(cap, is_group):
            continue
        if for_prompt and not cap.in_prompt:
            continue
        (enabled if is_enabled(cap, config) else disabled).append(cap)
    enabled.sort(key=lambda cap: since_of(cap), reverse=True)
    recent = [
        cap
        for cap in enabled
        if (today - since_of(cap)).days <= RECENT_DAYS
    ][:MAX_RECENT_IN_PROMPT]
    old = [cap for cap in enabled if cap not in recent]
    return recent, old, disabled


def prompt_block(config: Any, *, today: date | None = None, is_group: bool = False) -> str:
    """给 system prompt 的静态块。同一个进程里只随开关变化，不随每轮时间变化。"""
    today = today or date.today()
    recent, old, disabled = _split_recent(config, today, is_group, for_prompt=True)

    lines = ["【你这一版会什么】"]
    if recent:
        lines.append(
            "最近学的："
            + "｜".join(f"{cap.skill}（{_recent_label(cap, today)}）" for cap in recent)
        )
    else:
        lines.append("最近学的：这几天没学新东西")
    if old:
        lines.append("其他会的：" + "、".join(cap.label for cap in old))
    if disabled:
        lines.append(
            "现在关着："
            + "、".join(
                f"{cap.label}（说「{cap.on}」开）" if cap.on else cap.label
                for cap in disabled
            )
            + "。关着就是没开，不是坏了；主人打开前别答应。"
        )
    lines.append("你做不到：" + "；".join(LIMITS))
    lines.append(
        "有人问你「会什么 / 最近更新了什么」：挑一两件跟眼前话题有关的说，"
        "用你自己的话；别背清单、别报版本号，也别主动播报更新。"
    )
    return "\n".join(lines)


def owner_text(config: Any, *, today: date | None = None, is_group: bool = False) -> str:
    """主人发「更新」时看到的清单。0 token，按当前开关实时算。"""
    today = today or date.today()
    recent, old, disabled = _split_recent(config, today, is_group)

    lines = [f"📋 本鱼这一版会什么（{today.strftime('%m-%d')}）"]
    if recent:
        lines.append("【最近学会】")
        for cap in recent:
            lines.append(f"- {_recent_label(cap, today)}：{cap.skill}")
    else:
        lines.append("【最近学会】这几天没学新东西")

    if old:
        lines.append("【其他会的】" + "、".join(cap.label for cap in old))
    if disabled:
        lines.append("【现在关着】" + "、".join(cap.label for cap in disabled))
    else:
        lines.append("【现在关着】没有，都开着")
    lines.append("【做不到】" + "；".join(LIMITS))
    lines.append("（发「指令」看你能调什么）")
    return "\n".join(lines)


def status_line(config: Any) -> str:
    """「状态」里的【功能】行。只读，按当前 config 拼。"""
    bits = []
    for cap in STATUS_FEATURES:
        bits.append(f"{cap.short or cap.label} {'开' if is_enabled(cap, config) else '关'}")
    return "【功能】" + "｜".join(bits)


def markdown_table() -> str:
    """`docs/能力表.md` 的完整内容。`tools/check_capabilities.py --write` 生成。"""
    lines = [
        "# 能力表：大肥鱼这一版会什么",
        "",
        "> 这份表由 [`qqbot/capabilities.py`](../qqbot/capabilities.py) 生成，**别手改**。",
        "> 手写条目在 `MANUAL_CAPABILITIES`，提交 trailer 自动登记的条目在",
        "> `qqbot/capabilities_auto.json`；两种都汇总到这里。",
        "> 完整流程（trailer 字段、校验、pre-commit）见",
        "> [`docs/能力表工作流.md`](能力表工作流.md)。",
        ">",
        "> ```powershell",
        "> python tools/check_capabilities.py --write",
        "> ```",
        "",
        "## 她会什么",
        "",
        "| key | 她自己的说法 | 学会 | 开关 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for cap in CAPABILITIES:
        switch = ""
        if cap.flag:
            switch = f"`{cap.flag}`"
            if cap.off and cap.on:
                switch += f"（{cap.off} / {cap.on}）"
        note = cap.note or ("只在主人私聊里" if cap.audience == "owner" else "")
        lines.append(
            f"| `{cap.key}` | {cap.skill} | {cap.since} | {switch} | {note} |"
        )
    lines += [
        "",
        "## 她做不到",
        "",
    ]
    lines += [f"- {item}" for item in LIMITS]
    lines += [
        "",
        "## 怎么加一条",
        "",
        "### 手写（推荐给需要斟酌措辞的）",
        "",
        "1. 在 `MANUAL_CAPABILITIES` 里加一个 `Capability`，`skill` 用她自己的口吻，",
        "   不要写代码名。",
        "2. 有开关就填 `flag`；主人能在聊天里开关的，再填 `off` / `on`，",
        "   而且这两个说法必须能被 `qqbot/commands.py::parse()` 认出来。",
        "3. 跑 `python tools/check_capabilities.py --write` 重新生成本表。",
        "4. 跑 `python tools/test_capability_table.py` 和 `python tools/check_command_spec.py`。",
        "5. 重启机器人才生效（`_system_prompt` 在启动时就把「最近学会」定下来了）。",
        "",
        "### 自动（commit trailer）",
        "",
        "在 commit message 末尾写：",
        "",
        "```text",
        "Capability: 别人转发的聊天记录，你能读到里面的话",
        "Capability-Key: forward",
        "Capability-Flag: enable_forward",
        "Capability-Label: 合并转发",
        "Capability-Off: 关闭合并转发",
        "Capability-On: 打开合并转发",
        "```",
        "",
        "然后：",
        "",
        "```powershell",
        "python tools/capability_sync.py --apply",
        "python tools/install_git_hooks.py   # 第一次装 pre-commit",
        "```",
        "",
        "`capability_sync.py` 会校验并写 `capabilities_auto.json`；纯人设提交不参与。",
        "",
    ]
    return "\n".join(lines)
