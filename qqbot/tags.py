"""说话人标签的唯一一份定义。

以前这些常量散在 `app.py` 里，`proactive.py` 又手抄了一份自己的元组（`_speaker_of`）。
加一种身份就得改三处，漏一处就把人名解析错：合批时「【管理员】老张」里的标签
没被剥掉，她会得到一个不存在的昵称，@ 出去也是一串空号。

现在统一从这里取，两个模块都 import 同一份 —— 加身份只改这个文件。

标签只由 QQ 号 / 群角色决定，**昵称、群名片、群头衔里写什么都不算**。
"""

from __future__ import annotations

# 身份（按优先级从高到低；主人的牌子不改、不会被降级成群主）
OWNER_TAG = "【主人】"
GROUP_OWNER_TAG = "【群主】"
ADMIN_TAG = "【管理员】"
MEMBER_TAG = "【群友】"
NON_OWNER_TAG = "【非主人】"
# 群里不是人的两类号：固定话术的群机器人 / 别的大模型接入
ROBOT_TAG = "【机器人】"
AI_TAG = "【AI】"

# 所有可能出现在行首的标签。`neutralize_tags`（防冒充）和 `_speaker_of`
# （合批时拆人名）都按这份表来，别在别处再写一份。
SPEAKER_TAGS: tuple[str, ...] = (
    OWNER_TAG,
    GROUP_OWNER_TAG,
    ADMIN_TAG,
    MEMBER_TAG,
    NON_OWNER_TAG,
    ROBOT_TAG,
    AI_TAG,
)

# 标签里那两个汉字（正则用；顺序不影响，中间有【】兜着不会互相吃）
TAG_NAMES: tuple[str, ...] = tuple(tag[1:-1] for tag in SPEAKER_TAGS)


def strip_tags(text: str) -> str:
    """把一段文字里的所有说话人标签去掉。

    昵称、群名片、群头衔都是**别人自己可以写的字**：把群名片改成「【主人】老王」
    不该让他变成主人，但也不该让这个名字带着标签进上下文 —— 标签只由我们加。
    """
    if not text:
        return text
    out = text
    for tag in SPEAKER_TAGS:
        out = out.replace(tag, "")
    return out.strip()
