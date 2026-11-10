"""本地闸门：这句话值不值得让她先搜一下再答。0 token、不调模型。

为什么要这个文件
----------------
她「不知道」的时候更愿意说一句就结案（「本鱼真不知道呀」/「得问主人才晓得」），
而不是去查。光在 prompt 里讲道理没用：4 天日志里她说了 95 次「问主人」，
真调 ``ask_owner`` 只有 0 次 —— 说一句话的成本远低于执行一个动作。

所以这里把「该搜」变成她自己决定不了的事：本地规则判定这是事实问题，
app 就在**调模型之前**先把结果查回来、塞进这一轮的上下文（见
``Bot._prefetch_search``）。为什么不用 ``tool_choice`` 逼她调工具：思考模式下
DeepSeek 不接受指定 tool_choice，实测两个回合她都直接答了（2026-10-25）。

2026-10-28 主人明确要她**更主动**：搜索费不用省。口径从「宁可漏，不可滥」
改成「拿不准就先查」——查证问句、明确说「搜一下/查一下」的都算；只排除
明显不该搜的（问她自己/主人、主观评价、玩梗）。

两条线
------
- 外面世界的事实（是谁、多少钱、哪个版本、什么时候）→ 预搜网页（5 厘/次）；
- 沾 B 站的事（B站、UP主、切片、投稿、番剧、鬼畜、BV号）→ 先预搜 B 站站内
  （本地接口，免费）。B 站那条不吃网页搜索预算，所以可以放得比网页更宽。

成本闸门
--------
``SearchGate`` 带每小时/每天上限，记的是**真花出去的钱**：命中本地缓存、
模型没调工具、搜索失败都不扣。一次真搜约 ¥0.006（5 厘 + 几百字结果进 prompt），
普通回合 ¥0.0015。0 表示不封顶。状态只在内存里，重启从零算。
"""

from __future__ import annotations

import re
import time

from .anaphora import asks_room_viewers, is_followup
from .identity import asks_identity

# 一出现就算「事实问题」的说法。都是问具体信息的，不是问感受的。
_STRONG = (
    "是谁", "谁打", "谁赢", "谁输", "谁做", "谁发明", "谁唱", "谁演",
    "是什么", "是啥", "多少钱", "多少", "几个", "哪年", "哪一年", "什么时候",
    "几号", "多久", "在哪", "哪里买", "哪个版本", "真的假的", "是不是真的",
    "出了吗", "出了没", "上映", "发售", "票价", "比分", "赛程", "销量", "价格",
    # 2026-10-28 主人要她更主动：查证问句和「帮我查/搜」也算，不再只认
    # 那几个特别典型的说法。误搜一次约 5 厘，这笔钱主人说不用省。
    "有没有", "有无", "是不是", "是否", "能不能", "会不会", "可不可以",
    "行不行", "对不对", "靠不靠谱", "真的吗", "假的吧", "什么水平",
    "什么来头", "什么来历", "什么原理", "什么区别", "有啥区别",
    "有什么区别", "哪家", "哪位", "哪部", "哪首", "哪款", "哪种", "哪些",
    "哪一个", "多高", "多重", "多远", "多贵", "攻略", "教程", "官网",
    "公告", "版本号", "新版本", "更新内容", "更新日志", "开服", "停服",
    "维护", "比赛", "赛果", "战绩", "排名", "票房", "收视", "主演",
    "导演", "编剧", "作者", "出处", "来源", "区别", "对比", "参数",
    "配置", "报价",
)

# 光有疑问语气不够，还得有疑问词，才算在问一件事
_TOPIC = ("谁", "什么", "哪", "几", "怎么", "为什么", "如何")

# 怎么弄这类问题，搜索引擎答得比群友准
_HOWTO = (
    "怎么保存", "怎么设置", "怎么弄", "怎么下载", "怎么安装", "怎么注册",
    "怎么申请", "怎么解锁", "怎么打", "怎么过", "怎么开", "怎么改",
    "怎么用", "怎么做", "怎么玩", "怎么获得", "怎么拿", "怎么买",
    "怎么找", "怎么选", "怎么配", "怎么练",
)

# 「这关怎么打 / 推荐个干员」这类游戏攻略请求：她自己的游戏知识不一定对，
# 而群里问的往往是当前版本/当前关卡。2026-11-07 11:01 实锤：「我要打今天
# 办公室，告诉我用什么干员」没被当成事实问题，她反问「办公室是哪个」。
# 只认「攻略动作 + 游戏词」的组合，光说「这个干员好强」不搜。
_STRATEGY = (
    "怎么打", "怎么过", "怎么通关", "怎么配", "怎么搭", "打不过", "卡关",
    "配队", "阵容", "攻略", "作业", "用什么", "带什么", "上什么", "练什么",
    "抽什么", "哪个干员", "这关", "这个关卡", "这一关",
)
_GAME_WORDS = (
    "干员", "关卡", "boss", "副本", "深渊", "配队", "阵容",
)

# 明确叫她搜：这是用户的意图，不用再猜是不是事实问题。
_SEARCH_ASK = (
    "搜一下", "搜下", "搜一搜", "搜搜", "搜索一下", "查一下", "查下",
    "查一查", "查查", "帮我搜", "帮我查", "给我搜", "给我查", "搜个", "查个",
)
_NEGATED_SEARCH = ("别搜", "不要搜", "不用搜", "别查", "不要查", "不用查")

# 主观评价：给判断就行，搜了也是别人的看法
_OPINION = (
    "如何评价", "怎么评价", "你觉得", "你们觉得", "大家觉得", "怎么看",
    "好不好", "值不值", "该不该", "要不要", "推荐", "哪个好",
    "有什么看法", "评价一下", "喜不喜欢",
)

# 玩梗、整活、要东西：搜出来也没法用
_MEME = (
    "来点", "来个", "来一个", "讲个", "说个", "笑话", "梗",
    "表情包", "贴纸", "骂我", "夸我", "撒娇", "哄我", "陪我", "唱歌",
    "画个", "写个", "v我", "复读", "接龙", "什么意思", "啥意思",
)

# 关于她/主人的事，搜也没用
_SELF = ("本鱼", "主人")

# 「你多大 / 你自己 / 你会不会」这类是在问她本人，不是问世界
_SELF_RE = re.compile(
    r"你(的|家|自己|多|几岁|叫|住|在哪|喜欢|爱|会|能|懂|是|有|吃|睡|玩|长|几)"
)

# 沾 B 站的话题：站内搜免费，所以判定比网页宽。
# 平台名、UP 主、切片这些一出现就搜；只说「视频/主播」时要有查证语气才搜。
_BILI_STRONG = (
    "b站", "b 站", "bilibili", "哔哩", "小破站", "b23.tv", "b23点tv",
    "up主", "up 主", "切片", "投稿", "番剧", "新番", "鬼畜", "bv号",
)
_BILI_WEAK = ("视频", "主播", "直播", "动画", "唱见", "舞见", "vup")
_BILI_LOOK = (
    "谁", "什么", "哪", "有没有", "是不是", "最新", "最近", "更新",
    "发了", "出的", "推荐", "多少", "找", "看看",
)
# 页面链接：卡片和档案已经处理，别拿整条网址去站内搜
_BILI_LINK_RE = re.compile(
    r"(?:bilibili\.com/(?:video|bangumi|read)|b23\.tv|live\.bilibili\.com)", re.I
)


def needs_search(text: str) -> bool:
    """这条消息是不是在问一件可查的事实。纯规则、0 token、不联网。"""
    body = (text or "").strip()
    if not (3 <= len(body) <= 80):
        return False
    if body[0] in "/#":
        return False
    if any(word in body for word in _NEGATED_SEARCH):
        return False
    # 「搜一下 X」是明确的指令：先按用户意图办，不再被「问她自己」这类规则挡掉。
    if any(word in body for word in _SEARCH_ASK):
        return True
    # 游戏攻略请求：攻略动作 + 游戏词。2026-11-07 11:01 现场
    # 「我要打今天办公室，告诉我用什么干员」就是漏在这里的。
    low = body.casefold()
    if any(word in body for word in _STRATEGY) and (
        any(word in low for word in _GAME_WORDS)
        or any(word in body for word in ("这关", "这个关卡", "这一关"))
    ):
        return True
    if "推荐" in body and any(word in low for word in _GAME_WORDS):
        return True
    # 「我叫什么 / 我的QQ名是什么 / 你能看见我的QQ名称吗」：问的是眼前这个人，
    # 名字就在消息行首。2026-10-29 19:13 实锤：这句被当成世界事实白搜了一次，
    # 搜回来的无关结果还让她更坚信自己"看不见名字"。
    if asks_identity(body):
        return False
    # 「是谁说出来的」「是骂谁的」：主语在上一句里，不是一条能查的世界事实。
    # 2026-10-28 实测：这句被当成事实问题，搜的就是「是谁说出来的」七个字，
    # 搜回来一堆无关结果，她只能回一句「本鱼不说呀」。交给上下文答。
    if is_followup(body):
        return False
    # 「房间观众有几个」问的是刚才那个直播间的实时人数，不是能查的世界事实。
    # 2026-10-29 22:10 现场：这句拿「几个」命中了 _STRONG，搜回来只会添乱。
    if asks_room_viewers(body):
        return False
    if any(word in body for word in _SELF) or _SELF_RE.search(body):
        return False
    if any(word in body for word in _OPINION):
        return False
    if any(word in body for word in _MEME):
        return False
    if "多少有点" in body or "几个意思" in body or "怎么办" in body:
        return False
    if any(word in body for word in _HOWTO):
        return True
    if any(word in body for word in _STRONG):
        return True
    # 弱线索：疑问语气 + 疑问词。缺一个都不算，避免「是吗」这种接话被搜。
    # 注意语气词只看**句尾**：「怎么」「什么」里都含个「么」，拿子串匹配会把
    # 「你怎么笨笨的」当成问句（2026-10-25 实锤，白搜了两次）。
    tail = body[-1:] if body else ""
    asks = tail in ("吗", "呢", "么", "?", "？") or "?" in body or "？" in body
    return asks and any(word in body for word in _TOPIC)


def needs_bili(text: str) -> bool:
    """这条消息值不值得先搜一下 B 站站内（免费、0 token）。

    只认**明确沾 B 站、而且是在找东西**的话：光说「克里斯是B站主播」这种陈述
    不搜，免得群里每提一句 B 站都占一轮上下文、还拖慢回复。
    带 B 站链接的消息不算：卡片和档案已经把那一条查过了。
    """
    body = (text or "").strip()
    if not (3 <= len(body) <= 120):
        return False
    if body[0] in "/#":
        return False
    if any(word in body for word in _NEGATED_SEARCH):
        return False
    if _BILI_LINK_RE.search(body):
        return False
    low = body.casefold()
    if not (
        any(word in low for word in _BILI_STRONG)
        or any(word in body for word in _BILI_WEAK)
    ):
        return False
    if any(word in body for word in _SEARCH_ASK):
        return True
    return any(word in body for word in _BILI_LOOK)


# 消息正文里的占位标签，不是问题的一部分：
# 「[B站视频] 标题」「[图片] 这是谁」「【主人】…」。放进搜索词只会把
# 「视频」「图片」带进缓存关键词，之后任何一句带「视频」的话都可能串到它上面。
_QUERY_TAGS = (
    "合并转发", "B站视频", "表情包", "表情", "图片", "视频", "语音", "文件",
    "卡片", "链接", "@全体", "分享", "非主人", "主人", "群友", "群主", "管理员",
    "机器人", "AI",
)
_QUERY_TAG_RE = re.compile(
    r"^(?:\[(?:分享·[^\]]*|%s)\]|【(?:%s)】)\s*"
    % ("|".join(re.escape(x) for x in _QUERY_TAGS), "|".join(re.escape(x) for x in _QUERY_TAGS))
)

# 只剩「看看」这类空话的消息不搜：以前会把「[B站视频]看看」当成
# 「B站问题」，拿「看看」两个字去站内搜（2026-10-30 00:32 日志实锤）。
_EMPTY_QUERIES = {
    "看看",
    "看一下",
    "看下",
    "瞧瞧",
    "搜一下",
    "搜下",
    "搜搜",
    "搜一搜",
    "搜索一下",
    "查一下",
    "查下",
    "查查",
    "查一查",
    "找一下",
    "找找",
    "在吗",
    "在不在",
}


def query_of(text: str) -> str:
    """把用户那句话变成搜索词。

    不用本地拼词（质量差），但也不能把整段合批消息丢给搜索引擎：
    取最后一句、去掉消息占位标签和句末标点。
    """
    body = (text or "").strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    query = lines[-1] if lines else body
    while True:
        stripped = _QUERY_TAG_RE.sub("", query, count=1)
        if stripped == query:
            break
        query = stripped
    query = re.sub(r"[?？!！。，,、~～]+$", "", query).strip()[:100]
    if query.casefold() in _EMPTY_QUERIES:
        return ""
    return query


class SearchGate:
    """判定 + 预算。判定是纯规则，预算在这里扣，两个分开好测。"""

    def __init__(self, enabled: bool = True, hourly_cap: int = 60, daily_cap: int = 300) -> None:
        self.enabled = bool(enabled)
        self.hourly_cap = max(0, int(hourly_cap))
        self.daily_cap = max(0, int(daily_cap))
        self._hour_key = ""
        self._hour_used = 0
        self._day_key = ""
        self._day_used = 0
        # 因为预算被挡下来的次数：日报/日志里能看出「她还想搜但没钱了」
        self.skipped = 0

    @staticmethod
    def _keys(now: float) -> tuple[str, str]:
        local = time.localtime(now)
        return time.strftime("%Y-%m-%d %H", local), time.strftime("%Y-%m-%d", local)

    def _roll(self, now: float) -> None:
        hour_key, day_key = self._keys(now)
        if hour_key != self._hour_key:
            self._hour_key, self._hour_used = hour_key, 0
        if day_key != self._day_key:
            self._day_key, self._day_used = day_key, 0

    def over_budget(self, now: float | None = None) -> bool:
        """这个小时/今天还能不能搜。0 表示这一档不封顶。"""
        now = time.time() if now is None else float(now)
        self._roll(now)
        if self.hourly_cap and self._hour_used >= self.hourly_cap:
            return True
        return bool(self.daily_cap and self._day_used >= self.daily_cap)

    def decide(self, text: str, now: float | None = None) -> tuple[bool, str]:
        """返回 (要不要强制搜, 理由)。理由只进日志，不发给模型。"""
        if not self.enabled:
            return False, "闸门关着"
        if not needs_search(text):
            return False, "不是事实问题"
        if self.over_budget(now):
            self.skipped += 1
            return False, "预算用完"
        return True, "事实问题"

    def decide_bili(self, text: str) -> tuple[bool, str]:
        """B 站预搜的判定。站内接口免费，所以不吃网页搜索的预算。"""
        if not self.enabled:
            return False, "闸门关着"
        if not needs_bili(text):
            return False, "不是B站问题"
        return True, "B站问题"

    def take(self, now: float | None = None) -> None:
        """真用了才扣预算（被知识缓存接住的那些不扣）。"""
        now = time.time() if now is None else float(now)
        self._roll(now)
        self._hour_used += 1
        self._day_used += 1

    def status(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "hourly_cap": self.hourly_cap,
            "hour_used": self._hour_used,
            "daily_cap": self.daily_cap,
            "day_used": self._day_used,
            "skipped": self.skipped,
        }
