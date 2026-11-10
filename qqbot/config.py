"""读取 .env 配置。故意不引第三方库，少一个依赖少一个坑。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 运行时数据库 / 状态 / 日志都收在这里；代码里仍然用 ROOT / 相对路径 拼，
# 所以 Config 里带目录的值必须保持“相对仓库根”的写法（如 data/memory.db）。
DATA_DIR = ROOT / "data"


def _file_setting(name: str, default: str) -> str:
    """文件类配置：裸文件名自动收进 data/，带目录的路径保持原样。

    这样旧的 .env（MEMORY_DB=memory.db）不用改也能找到 data/memory.db；
    测试里显式写 "_test_mem/x.db" 的路径也不会被改写。
    """
    value = (os.environ.get(name, default) or "").strip() or default
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path)
    if path.parent in (Path("."), Path("")):
        return (DATA_DIR / path).relative_to(ROOT).as_posix()
    return path.as_posix()


def load_env(path: Path | None = None) -> None:
    """.env -> os.environ，不覆盖已存在的真实环境变量。"""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]


@dataclass
class Config:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: str = "disabled"
    dry_run: bool = False

    ws_url: str = "ws://127.0.0.1:3001"
    token: str = ""

    bot_qq: str = ""
    persona: str = ""
    trigger_at: bool = True
    trigger_prefix: list[str] = field(default_factory=list)
    group_whitelist: list[str] = field(default_factory=list)
    # 手工维护：群里哪些号是固定话术的机器人 / 别的大模型接入
    # 只影响标签与语料，不影响谁能触发她
    robot_qqs: list[str] = field(default_factory=list)
    ai_qqs: list[str] = field(default_factory=list)
    # 名单落盘的位置（QQ 指令改完写这里，重启后以它为准）
    speakers_file: str = "data/speakers.json"
    allow_private: bool = True
    max_history: int = 12
    rate_limit: float = 3.0
    # 消息从发出到现在超过这么久才算"积压"（电脑休眠后回连会一次涌进来）
    stale_message_seconds: int = 120
    # 分句发：她自己换行的地方，分成两条消息发出去
    enable_split_messages: bool = True
    split_delay_min: float = 0.4
    split_delay_max: float = 1.0
    split_max_segments: int = 3
    # 答完再补一句：她答完一个问题后，隔一会儿再往前推一句
    enable_followup: bool = True
    followup_delay_min: int = 90
    followup_delay_max: int = 240
    followup_cooldown_minutes: int = 30
    max_reply_chars: int = 600

    # 联网搜索（DeepSeek 独立接口，5 元 / 1000 次）
    enable_search: bool = True
    # 本地闸门：她判定「不知道」之前，先自己查一次的规则（0 token，见 searchgate.py）。
    # 2026-10-28 起口径是「拿不准就先查」：查证问句、明确说「搜一下」都算；
    # 沾 B 站的话题还会免费预搜站内，不吃这里的网页预算。
    # 记的是真花出去的钱，命中缓存/失败/超时都不扣；0 = 那一档不封顶。
    enable_search_gate: bool = True
    search_gate_hourly: int = 60
    search_gate_daily: int = 300
    # 查过的问题多久以内直接复用缓存（分钟），0 = 每次都真搜。
    # 主人说搜索费不用省，所以默认只复用半小时；更久的一律重新搜。
    search_reuse_minutes: float = 30.0
    # B站链接：收到分享时顺手查标题/UP/时长/播放/简介（本地 HTTP，0 token）。
    # 只解决「她不知道你发的是什么」，不下载视频、不看画面、不碰弹幕评论。
    enable_bili_card: bool = True
    # 查一个链接最多等多久（秒）。超时/失败都当没查到，绝不影响这轮回复。
    bili_timeout: float = 4.0
    # 同一个视频查过一次留多久（分钟）：群里同一个链接会被反复贴
    bili_cache_minutes: int = 60
    # 卡片长度上限：正文是每轮都要发进 prompt 的，太长会挤掉人设和上下文
    bili_card_max_chars: int = 200
    # B站封面：查到视频后把封面图也给她看一眼（图走 vision，按图计费；只对 B 站链接生效）
    enable_bili_cover: bool = True
    bili_cover_max_mb: float = 1.5
    # B站档案：WBI 签名 + 小号 cookie，拿官方 AI 总结和字幕，落盘后复用（0 token，见 bili_archive.py）。
    # 没填 BILI_SESSDATA 也能跑：只是拿不到总结/字幕，不会报错。
    enable_bili_archive: bool = True
    bili_sessdata: str = ""
    bili_archive_dir: str = "data/bili"
    bili_archive_days: int = 7
    bili_archive_max_chars: int = 700
    # 一步到位：带链接那一轮最多同步等档案这么久（秒）；0 = 只给卡片，后台抓
    bili_sync_timeout: float = 12.0
    # 画面：被问「画面/长什么样/截图」时按镜头抽 12 格拼一张图（要下载视频，默认关）
    enable_bili_frames: bool = False
    bili_frames_timeout: float = 60.0
    bili_frames_cells: int = 12
    # 画面工具：除了关键词路径，模型也可以自己判断「这句需要看画面」再调
    # bili_frames（下载视频 + vision 计费）。按天限额，0 = 不封顶；
    # 太长的视频不抽（下载/抽帧会超时）。
    enable_bili_frames_tool: bool = True
    bili_frames_tool_daily: int = 6
    bili_frames_tool_max_minutes: int = 40
    # 追问「03:10 那段」时用留底单帧放大；留底默认 24 小时，过期自动删
    enable_bili_zoom: bool = True
    bili_frames_keep_hours: float = 24.0
    # 档案里的「热评 / 弹幕峰值」：多 1~5 个请求，仍在后台跑；风控严时可关
    enable_bili_extras: bool = True
    # 档案学到的内容也写进知识库（本地 sqlite，0 token）：以后聊到同一个视频/UP
    # 能想起来。文本压到 400 字以内，TTL 比搜索结果长。
    enable_bili_knowledge: bool = True
    bili_knowledge_days: int = 30
    # 接口底座：单次超时 / 两次请求最小间隔（秒）/ 一次档案最多跑多久
    bili_api_timeout: float = 6.0
    bili_rate: float = 1.0
    bili_archive_timeout: float = 25.0
    # 直播陪看：只读房间标题 + 弹幕（公开 HTTP / WebSocket，0 token）。
    # 画面默认关；开了之后被问画面、或弹幕爆点时才拉 12 秒拼一张。
    enable_bili_live: bool = False
    bili_live_poll_seconds: int = 60
    bili_live_max_danmaku: int = 40
    bili_live_note_lines: int = 8
    bili_live_timeout: float = 6.0
    enable_live_frames: bool = False
    bili_live_frames_seconds: float = 12.0
    bili_live_frames_interval: float = 1.33
    # 滚动缓冲：看直播时每 15 秒拉 2 秒，最近 2 分钟留在内存；问画面时直接拼
    bili_live_buffer_enabled: bool = True
    bili_live_buffer_seconds: float = 120.0
    bili_live_buffer_interval: float = 15.0
    bili_live_peak_enabled: bool = True
    bili_live_peak_min: int = 8
    bili_live_peak_cooldown: float = 120.0
    # 发弹幕（写操作，默认关）：只发到正在陪看的那个房间，内容从本地克里斯语料挑。
    # 需要 BILI_JCT（csrf）；B站普通用户弹幕上限 20 字，超了接口直接拒。
    enable_live_danmaku: bool = False
    bili_jct: str = ""
    bili_live_danmaku_max_chars: int = 20
    bili_live_danmaku_interval: float = 90.0
    bili_live_danmaku_daily: int = 10
    bili_live_danmaku_sent_file: str = "data/live_danmaku_sent.json"
    # 弹幕库：原始采样 + 高频词聚合；弹幕不自动进群梗表，主人「直播梗入库」才写。
    enable_bili_live_memes: bool = True
    bili_live_db: str = "data/live_danmaku.db"
    bili_live_raw_days: int = 7
    bili_live_term_days: int = 30
    bili_live_sample_rate: float = 0.1
    bili_live_min_hits: int = 3
    bili_live_min_speakers: int = 2
    # 开播/下播记录：陪看轮询时顺手记，重启不丢；查询只读本地库，0 token。
    # 和弹幕库分开存，关掉直播梗也不影响开播记录。
    bili_live_history_db: str = "data/live_history.db"
    bili_live_history_days: int = 180
    # B站生活：她自己的小号，后台刷首页/兴趣、留生活记忆；写操作另开。
    # 和聊天里的 BILI_SESSDATA 分开，换号/换口味不会影响群里的链接档案。
    enable_bili_life: bool = False
    bili_life_sessdata: str = ""
    bili_life_jct: str = ""
    bili_life_dir: str = "data/bili_life"
    bili_life_window_start: int = 10
    bili_life_window_end: int = 23
    bili_life_daily_runs: int = 10
    bili_life_daily_picks: int = 30
    bili_life_max_archives: int = 3
    bili_life_searches_per_run: int = 3
    bili_life_daily_searches: int = 30
    bili_life_min_gap_hours: int = 1
    bili_life_bored_minutes: int = 45
    bili_life_vision: bool = False
    bili_life_vision_daily: int = 10
    bili_life_vision_max_videos: int = 1
    bili_life_vision_max_grids: int = 3
    bili_life_min_views: int = 1000
    bili_life_max_duration: int = 1200
    bili_life_vision_max_duration: int = 1200
    bili_life_topics: tuple[str, ...] = ()
    bili_life_interactions: bool = False
    bili_life_comment: bool = False
    bili_life_like_daily: int = 18
    bili_life_collect_daily: int = 9
    bili_life_follow_daily: int = 3
    bili_life_comment_daily: int = 4
    bili_life_tell_daily: int = 1
    bili_life_fav_folder_id: int = 0
    # 克里斯评论语料：独立 CLI 全量采集（不进聊天链路，见 docs/克里斯语料.md）。
    # 默认不开；要跑时手动 python tools/chris_cli.py crawl。
    chris_mid: str = "10000000"
    chris_db: str = "data/chris_comments.db"
    chris_sessdata: str = ""
    # BiliSpider 的建议频率：基础 1.5s + 0~1s 抖动（实测稳定，约 2s/次）
    chris_rate: float = 1.5
    chris_stop_412: int = 5
    # 克里斯笑话反馈：群友笑了/嫌老，下一轮挑笑话才用（本地 0 token）
    enable_chris_feedback: bool = True
    chris_feedback_file: str = "data/chris_feedback.json"
    # 图像理解（deepseek-flash 支持 JPEG/PNG/GIF/WebP）
    enable_vision: bool = True
    # 图片记忆：同一张图只让模型看一次
    enable_image_cache: bool = True
    image_cache_db: str = "data/image_cache.db"
    image_cache_limit: int = 2000
    max_images: int = 4
    max_image_mb: float = 8.0
    # 图片和文字分两条发（先甩图再问「这是你吗」，或者反过来）时：
    # 图最多留这么久，等下一句点名时接上；0 = 不接
    image_context_seconds: float = 60.0
    # 被叫到、手上又没图时，多等这么久看对方是不是紧跟着补一张（0 = 不等）
    image_wait_seconds: float = 2.5
    # 合并转发：展开成文字再交给模型。群里仍按 @/主动插话门控，不强制回。
    enable_forward: bool = True
    forward_max_depth: int = 3
    forward_max_nodes: int = 200
    forward_max_chars: int = 8000
    forward_max_calls: int = 8
    # 放进群聊缓冲的预览长度（不是给模型的全文）
    forward_preview_chars: int = 300
    # 主人 QQ：只有这个号私聊时，才允许她改自己的签名/头像/昵称
    owner_qq: str = ""
    # 好友申请：默认关。开了之后只批「聊过」以上的人，一天最多几个，批了也不先说话。
    enable_friend_accept: bool = False
    friend_daily_cap: int = 3
    # 需要多熟才批：0 生人 / 1 眼熟 / 2 聊过 / 3 熟人 / 4 老熟人（见 qqbot/familiar.py）
    friend_min_tier: int = 2
    # 她主动删人：只在私聊里删「正在骚扰她的那个人」，一天最多几个
    friend_delete_daily: int = 1
    friend_file: str = "data/friends.json"
    # 别人拉她进群的待办：问完情况 → 报给主人 → 主人点头才进（重启不丢）
    group_invite_file: str = "data/group_invites.json"
    # 她自己拉黑人（私聊）：纯本地判断广告/骚扰，先警告几次，到数才拉黑。
    # 关掉之后只认主人的「别理他」。
    enable_auto_ban: bool = True
    auto_ban_warnings: int = 3
    # 警告回合的保质期（天）：太久以前的旧账不往一块儿算
    auto_ban_strike_days: int = 7
    # 图片审查：本地看不见图，只能用一次极短的看图调用（同一张图只判一次）
    auto_ban_vision: bool = True
    auto_ban_max_images: int = 2
    # 私聊里拒绝好友申请的兜底话术。
    # 好友申请按原因有内置委婉话术（刚认识/太久没见/好感为负/今天批够/开关关着），
    # 这里只是兜底；**不要写「主人说了」**，不把责任推给主人。
    friend_request_refusal: str = "我们才刚认识嘛，再多聊两句吧。"
    # 表情包：从 stickers/<情绪>/ 目录里挑图，跟在回复后面发
    enable_sticker: bool = True
    # 逐图语义索引：按本轮对话检索几张候选交给模型（写 [表情:#编号]）。
    # 需要 ENABLE_SEMANTIC 的本地 embedding；关掉就退回按标签随机。
    enable_sticker_index: bool = True
    # 每次给模型几张候选；0 = 不给菜单
    sticker_candidates: int = 6
    # 发不发的闸门：回复间隔 / 每小时配额 / 严肃语境否决。
    # 关掉就退回「模型想贴就贴」，频率旋钮只剩 0 有效。
    # 从群里捡表情包：只收「主角是她」的图，走完整入库流程
    enable_sticker_steal: bool = True
    # 每天最多收几张（0 = 关）；两次之间至少隔几分钟
    sticker_steal_daily: int = 3
    sticker_steal_min_minutes: float = 60.0
    # 原图上限；再大就放弃（入库要保留动图，不压缩）
    sticker_steal_max_mb: float = 3.0
    # 主人点名「偷这个」/「人工收图」时的上限：QQ 动图表情 3~8MB 很常见，
    # 按后台的 3MB 挡会一律「没取到图」；后台自动捡图仍然按上面那条挡
    sticker_steal_manual_max_mb: float = 10.0
    # 每天最多做几次「是不是她」的视觉判定（控制花销）
    sticker_steal_checks: int = 30
    # 本地颜色粗筛：蓝/黑白比例明显不像她的先不看；主人手动点名时不筛
    sticker_steal_color_filter: bool = True
    # 群友一起换掉有水印的表情包：引用旧图+新图，本地相似度通过才换
    enable_sticker_replace: bool = True
    # 每个群友每天最多换几张；0 = 不限；主人不计数
    sticker_replace_daily: int = 3
    sticker_replace_max_mb: float = 3.0
    # 生气的设定：同一件事被反复问，她自己会烦；烦到冷战就不回。
    # 怒气 0~100，按人记、按话题记；具体分档见 qqbot/mood.py 顶部
    enable_mood: bool = True
    mood_file: str = "data/mood.json"
    # 同一件事再问一遍加多少怒气
    mood_repeat_delta: float = 22.0
    # 命中雷点话题（AI、掉线这些）额外加多少
    mood_trigger_delta: float = 32.0
    # 每分钟自然消多少（不说话就自己消气）
    mood_decay_per_min: float = 2.5
    # 多久以内问的才算"同一件事"
    mood_window_minutes: int = 15
    # 冷战档硬拒时，真的不发的概率（其余情况回一句「……」）
    mood_silence_chance: float = 0.5
    # ---- 熟悉度（和好感度分开）----
    # 独立于群聊语料的日聚合：只存 uid/日期/条数，不存正文。
    # 群日志有 20 万行上限、按 id 删最旧；现算会让天数封顶，所以要单独存。
    familiar_db: str = "data/familiar.db"
    familiar_cache_seconds: int = 60
    # 超过这么多天没直接说话，不再按熟人口气提示（不改档位/分数）
    familiar_stale_days: int = 30
    enable_familiar_note: bool = True
    # ---- 好感度（会升会降，和熟悉度分开）----
    enable_affection: bool = True
    affection_file: str = "data/affection.json"
    # 每日免费额度：前这么多点全额，之后开始疲劳（不是硬性日上限）
    affection_free_gain: int = 5
    affection_free_loss: int = 10
    # 超过免费额度后，每 step 点，增量乘一次 factor（0.5 → 0.25 → …）
    affection_fatigue_step: int = 3
    affection_fatigue_factor: float = 0.5
    # 疲劳的最低折扣：再刷也还能涨，只是很慢
    affection_min_multiplier: float = 0.05
    # 总分硬护栏：-max ~ +max；到顶不再涨
    affection_score_max: int = 30
    # 多少天没有直接互动，向 0 靠 1 点
    affection_decay_days: int = 14
    # ---- 群名册（谁说话、什么身份）----
    # 消息事件自带 sender.role，所以"刚说话的人是不是群主"本来就 0 调用；
    # 群头衔 / 群主管理员名单才要拉一次 get_group_member_list，缓存这么久。
    roster_db: str = "data/roster.db"
    roster_ttl_seconds: int = 1800
    # 群友自己的权限状态（个人免打扰/称呼/别记我），和主人的 prefs 分开
    member_state_file: str = "data/member_state.json"
    # ---- @ 人（默认关：她只在"不点名会被当成在回别人"时才点）----
    enable_at: bool = False
    # 开 @ 之后，@ 谁由谁定：True = 模型自己挑（现状）；
    # False = 不让模型挑，只在「有人 @/引用她」时点回那个人（规则兜底）
    enable_at_model_choice: bool = True
    at_state_file: str = "data/at_state.json"
    # 同一个群两次 @ 至少隔多久（秒）
    at_group_cooldown: int = 300
    # 同一个群每小时最多点几次
    at_group_hourly_cap: int = 3
    # 同一个人两次被点至少隔多久（秒）。只算「她主动去点」那一路：
    # 回话和链里指代不数这个，改由「他没再开口就不再点」把关
    at_person_cooldown: int = 1800
    # 同一个人每天最多被「主动点名」几次。只算她主动去点那一路：
    # 回话和链里指代不占这个额度（2026-11-10：一场对话里点 8 次就点不出去了），
    # 对话由「他没再开口就不再点」把关
    at_person_daily_cap: int = 8
    # 他得在这么久以内说过话，才算"在场"（@ 一个不在场的人 = 纯骚扰）
    at_present_seconds: int = 300
    # 点了没回，这个人多久之内不再点（防追命连环 @；0 = 关掉这条）
    at_miss_ignore_hours: float = 24.0
    # 第一次见到这个群之后多久内不点名（先认认人）
    at_warmup_hours: float = 6.0
    # 雷点话题：人设里已经点过名的那些
    mood_trigger_words: list[str] = field(default_factory=list)
    # 长期记忆：对话落 SQLite，跨重启/跨天能想起旧事
    enable_memory: bool = True
    memory_db: str = "data/memory.db"
    # 长期记忆按什么划分：user = 按人（私聊/群聊共享同一份记忆），session = 按会话
    memory_scope: str = "user"
    # 群消息全量落库：风格学习/黑话挖掘的语料来源
    enable_group_log: bool = True
    group_log_db: str = "data/group_log.db"
    # 风格学习：学到的东西先只存不注入，注入要主人点头
    enable_style_learning: bool = False
    # 群风格统计画像：零模型，按群统计句长/标点/节奏；默认关，先预览再开
    enable_group_style: bool = False
    group_style_file: str = "data/group_style.json"
    group_style_refresh_minutes: int = 30
    group_style_sample: int = 400
    group_style_min_sample: int = 30
    group_style_max_chars: int = 300
    group_style_adopt: str = "观察为主"
    group_style_include_words: bool = True
    # 群语料理解层：从本群历史里捞相关旧消息，只当背景，不改 persona/口音。
    # 默认关：先用 tools/corpus_index.py 建索引、search 看召回，再决定开。
    enable_corpus_recall: bool = False
    corpus_index_db: str = "data/corpus_index.db"
    # 群语料索引用哪个 embedding；空 = 跟 EMBED_MODEL 一样。
    # 微调模型建议只给这里用，别动记忆/表情包那边，出问题好回滚。
    corpus_embed_model: str = ""
    corpus_recall_limit: int = 3
    corpus_recall_days: float = 30.0
    corpus_recall_max_chars: int = 260
    # 语义余弦下限；关键词命中另有 0.50 的门槛（见 corpusindex.py）
    corpus_recall_min_score: float = 0.36
    # 没有关键词命中、只靠语义时的门槛；短句余弦偏高，必须更严
    corpus_recall_semantic_only: float = 0.68
    corpus_recall_exclude_minutes: int = 10
    # 群语料索引增量同步：启动补一次 + 每 N 分钟把新群消息编码进索引。
    # 0 = 不跑定时（只保留启动那次）；词表启动时单独刷，全量 build 留给换模型/重建。
    enable_corpus_sync: bool = True
    corpus_sync_on_start: bool = True
    corpus_sync_minutes: int = 15
    # 启动补完增量后，如果 terms_stale 就只重算词表（不加载模型、不重算向量）
    corpus_terms_refresh_on_start: bool = True
    # 热梗知识库：每天抓一次贴吧/B站，触发词命中时递一小抄
    enable_hot_trends: bool = True
    hot_trends_file: str = "data/hot_trends.json"
    hot_trends_hour: int = 6
    hot_trends_check_minutes: int = 15
    hot_trends_keep_days: int = 7
    hot_trends_max_topics: int = 300
    hot_trends_max_chars: int = 480
    hot_trends_trigger_words: list[str] = field(default_factory=list)
    # 热点小抄的「为什么火」：每天抓完后调一次模型批量写，几分钱
    enable_trends_explain: bool = True
    trends_explain_count: int = 8
    # 跟着群友复读：同一短串被多人短时间复读时，她也接一次（默认严格限流）
    enable_repeat_along: bool = True
    repeat_along_min_people: int = 3
    repeat_along_min_repeats: int = 3
    repeat_along_window_seconds: float = 30.0
    repeat_along_cooldown_seconds: int = 300
    repeat_along_max_chars: int = 24
    repeat_along_per_hour: int = 6
    # 群梗表：从群日志统计「复读」，0 token；命中才注入，按群隔离
    enable_memes: bool = True
    # 聊天里模型搜到「可能是梗」时，收成结构化候选（不直接注入）
    enable_chat_meme_capture: bool = True
    memes_file: str = "data/memes.json"
    memes_keep_days: int = 30
    memes_max_per_group: int = 60
    memes_max_chars: int = 360
    # 梗的联网验证：群共识站住（跨天/多人/多次）才验；验过就不再花钱
    enable_meme_verify: bool = True
    meme_verify_per_day: int = 2
    meme_verify_per_run: int = 1
    meme_verify_count: int = 8
    meme_verify_min_speakers: int = 5
    meme_verify_min_hits: int = 8
    meme_verify_min_days: int = 3
    # 记梗前的待确认：主人发「记梗 X」→ 查一次 → 等「入库 / 不入库」
    meme_intake_file: str = "data/meme_intake.json"
    meme_intake_ttl_minutes: int = 120
    # 网络梗：没见过的短词先问群友，确认是梗后再搜一次入库
    enable_meme_ask: bool = True
    meme_ask_file: str = "data/meme_asks.json"
    meme_ask_ttl_minutes: int = 120
    meme_ask_cooldown_minutes: int = 10
    meme_ask_per_day: int = 5
    # 网络梗进 global 后留多久（天）；它是本地小百科，不按群梗 30 天保鲜
    memes_web_keep_days: int = 365
    # 写法归一：缩写/换字（f8fq、鱼八分钱）→ 正词的显式链接表
    meme_aliases_file: str = "data/meme_aliases.json"
    # 通用梗百科：只读本地词库；问梗给用法，平时提到也可提示怎么接
    enable_meme_wiki: bool = True
    meme_wiki_db: str = "data/meme_wiki.db"
    meme_wiki_max_chars: int = 220
    meme_wiki_mention: bool = True
    meme_wiki_cooldown_minutes: int = 10
    # 峰谷时段：工作日 9-12、14-18 高峰（北京时间），其余含周末/节假日空闲
    enable_pricetime_tier: bool = True
    pricetime_peak_tier: int = 0
    pricetime_offpeak_tier: int = 1
    pricetime_peak_windows: str = "09:00-12:00,14:00-18:00"
    pricetime_holidays: str = ""
    # 遗忘曲线：短期记忆按这个速率衰减（每天），低于阈值就不再召回
    forget_rate: float = 0.08
    forget_threshold: float = 0.15
    # 被想起这么多次之后，短期记忆转成长期
    promote_hits: int = 3
    # 转长期的门槛：两次"想起"至少隔这么多小时、关键词重合度、内容至少多长
    promote_min_gap_hours: float = 12.0
    promote_min_score: float = 0.5
    promote_min_chars: int = 12
    recall_limit: int = 5
    # 普通聊天至少给语义留几条：关键词再准也可能"字面命中、答非所问"。
    recall_semantic_floor: int = 1
    recall_min_score: float = 0.25
    # 回忆里最多带几条她自己的旧回复（0 = 只回忆别人说的）。
    # 她越聊越像自己是真实存在的：召回自己的口头禅 → 下一句照着说 → 再被召回。
    recall_self_max: int = 1
    # 记忆题（「你还记得…」「我上次说的…」）：本地识别后多取几条、看更长的上下文。
    # 这类问题不是在问世界真相，是在问"你记得什么"，值得多花一点 token。
    memory_question_recall: int = 6
    memory_question_turn_limit: int = 48
    memory_question_context_lines: int = 20
    # 记忆题时从 group_log 补"她当时没接"的群消息（0 = 不补）
    memory_group_log_days: int = 7
    memory_group_log_limit: int = 6
    # 每轮告诉她"你的记忆到哪为止、关于这个人有多少条事实"。
    # 她知道自己不知道什么，才不会用「本鱼笨」编一个解释。
    memory_status_note: bool = True
    # 给模型一个 remember_fact 工具：群聊/私聊都能记"关于当前说话人"的事实。
    enable_memory_tool: bool = True
    # 搜索结果缓存：上次查到的内容留几天（0 = 不缓存）
    knowledge_ttl_days: int = 7
    # 语义召回：本地 embedding，补关键词的短板
    enable_semantic: bool = True
    embed_model: str = "models/bge-small-zh-v1.5"
    semantic_z: float = 1.4
    semantic_cos: float = 0.35
    # 语义召回一次最多看多少条候选（按 id 倒序取最近这些）。
    # 群聊很活跃时 600 条可能只覆盖几个小时；3000 条一般能覆盖一两天。
    # 排序是 numpy 点积，3000 条也只多几毫秒，真正的成本是每次从 SQLite 读 6MB 向量。
    semantic_candidates: int = 3000
    # 语义命中也算"最近又碰到过"：给一点强度、把 last_access 往前挪。
    # 不然一条天天被语义想起的事，24 天后还是会按遗忘曲线消失。
    semantic_recall_bump: float = 0.05
    # 改口游戏：群友想让她改口时，慢一点、怂一点、给他们一点成就感。
    # 只演群里的梗/外号/玩笑，不碰主人定的事、她的设定、别人的隐私。
    enable_concession: bool = True
    concession_file: str = "data/concession.json"
    concession_ttl_seconds: int = 1800
    concession_min_gap_seconds: int = 15
    concession_stage_seconds: int = 20
    # QQ 空间
    enable_qzone: bool = False
    qzone_private: int = 0  # 0 公开，1 私密
    # 发说说前要不要先给主人过一遍（默认要）
    qzone_require_approval: bool = True
    # 发说说的频率闸：一天几条、两条隔多久、哪个时段不发
    qzone_daily_limit: int = 2
    qzone_cooldown_minutes: int = 60
    qzone_quiet_hours: str = "00:00-08:00"
    qzone_limits_file: str = "data/qzone_limits.json"
    # 让她自己想起来发说说：每天一次左右，从当天的聊天经历里挑一件事写
    enable_qzone_auto: bool = False
    qzone_auto_window: str = "18:00-23:00"   # 只在这个时段里"想起来"
    qzone_auto_min_hours: int = 20           # 两次之间的最小间隔
    qzone_auto_max_hours: int = 28           # 最大间隔（到点后随机挑）
    qzone_auto_min_turns: int = 4            # 今天至少有这么多条对话才值得写
    qzone_auto_check_minutes: int = 15       # 多久检查一次
    qzone_auto_state_file: str = "data/qzone_auto.json"
    # 回评论：别人在她自己发的说说下面评论，她自己去回一句（公开可见，节奏要慢）
    enable_qzone_reply: bool = False
    qzone_reply_check_minutes: int = 30      # 多久看一次
    qzone_reply_daily_limit: int = 3         # 一天最多回几条
    qzone_reply_cooldown_minutes: int = 10   # 两条回复之间至少隔多久
    qzone_reply_per_person_hours: int = 6    # 同一个人多久内只回一次
    qzone_reply_max_posts: int = 2           # 只看最近几条说说的评论
    qzone_reply_max_age_hours: int = 48      # 超过这么久的评论不补回
    qzone_reply_quiet_hours: str = "00:00-08:00"
    qzone_reply_ignore_uins: str = ""        # 这些号评论不回（逗号分隔）
    qzone_reply_state_file: str = "data/qzone_reply.json"
    qzone_reply_notify_owner: bool = False   # 回完要不要私聊告诉主人
    # 上线问候：启动完成后让模型按人设和当前时间写一句，发到所有群
    enable_startup_greeting: bool = True
    # 好友上线通知：好友私聊里自助订阅；只发上线，默认关（订阅名单空）
    presence_dm_enabled: bool = True
    presence_dm_stable_seconds: int = 60
    presence_dm_min_downtime_minutes: int = 30
    presence_dm_cooldown_minutes: int = 360
    presence_dm_daily_cap: int = 1
    presence_dm_quiet_hours: str = "23:00-08:00"
    presence_dm_max_recipients: int = 20
    presence_dm_state_file: str = "data/presence_dm_state.json"
    # 上线补收私聊：启动/重连后检查最近私聊，最后一条不是她说的就补回来。
    # 判断只看「最后一条是谁说的」：正常私聊都以她的话结尾，没结尾就是没接上。
    enable_catchup_dm: bool = True
    # 只补最近这么久以内的；超过就忽略，不对几天前的消息突然诈尸
    catchup_max_age_hours: float = 24.0
    # 连上后先稳定这么久再补，避开刚启动的连接抖动
    catchup_settle_seconds: float = 20.0
    catchup_max_contacts: int = 20
    catchup_history_count: int = 100
    # 一次上线最多真回几个（其余下次再补）；0 = 不封顶
    catchup_max_replies: int = 10
    # 联系人之间隔这么久，别一口气刷屏
    catchup_reply_spacing_seconds: float = 2.0
    # 漏了不到这么久就按正常语气接（仍会提一句刚卡了一下）
    catchup_apology_minutes: float = 10.0
    catchup_dry_run: bool = False
    # 群聊主动插话
    enable_proactive: bool = False
    # 风控规避模式：群聊慢回 + 频率门 + 主动功能全停。
    # 也存 prefs.feature_flags.risk_mode，聊天里「打开/关闭风控模式」切换。
    enable_risk_mode: bool = False
    # 说人话检查：生成后本地再复查一遍（作文腔叠在一起、跟同一会话里刚才那句太像），
    # 命中才让模型重说一次。只在群里跑 —— 私聊是人设说话的地方，口癖不算八股。
    # 也存 prefs.feature_flags.saycheck，聊天里「打开/关闭说人话」切换。
    enable_saycheck: bool = True
    # 响应档位：-1 表示不看这个字段，沿用下面的细项；0~3 见 app.py 的说明
    response_tier: int = -1
    proactive_keywords: list[str] = field(default_factory=list)
    # 关键词至少几个字（中文单字太容易在别的词里出现）
    proactive_keyword_min_len: int = 2
    proactive_chance: float = 0.03
    proactive_cooldown: int = 300
    # 有人喊「闭嘴」时安静多少分钟
    quiet_minutes: int = 5
    # 认「这句话在叫她」的名字：带了这些词才把「闭嘴」当成指令
    quiet_self_names: list[str] = field(default_factory=list)
    # 回复窗口：同一个人连发几条时，等这么久再一起回（0 = 关掉，逐条秒回）
    merge_window_seconds: float = 0.8
    # 发了链接/卡片之后多等这么久：人多半还会补一句「你看看这个」，
    # 不等就会先回卡片、再回留言（2026-10-26 实锤；0 = 不等）
    link_wait_seconds: float = 2.5
    # 连发合并的总等待封顶：打字再快也不等多于这么久（0 = 不封顶）
    merge_cap_seconds: float = 8.0
    # 群聊主动插话批次的封顶（和私聊解耦，避免改私聊窗口时误伤群聊）
    proactive_merge_cap_seconds: float = 8.0
    proactive_hourly_cap: int = 6
    # ---- 按人跟进窗口（P1）----
    # 她回了谁，就给谁开一个窗口；窗口内接着说的同一件事才继续接。
    proactive_followup_seconds: int = 90
    # 一条窗口里最多回几条（含开窗那条；0 = 不限制）
    proactive_followup_max_replies: int = 6
    # 她刚回完的同一个人，多少秒内下一句算"即时接话"（不要求话题重合）
    proactive_followup_immediate_seconds: int = 30
    # 两条回复之间的最小间隔
    proactive_followup_gap: int = 5
    # ---- 同类聊天（她跟别的 AI/bot 说话）----
    # 松：接不接看话题有没有往前走；紧：兜底一定要有（2026-10-27 主人定的）。
    peer_chat_enabled: bool = True
    # 同一只同类在这段时间里最多回几条（防一口气刷）
    peer_pair_max: int = 12
    peer_pair_window: int = 600
    # 每个群在这段时间里对同类最多回几条
    peer_group_max: int = 15
    peer_group_window: int = 3600
    # 一场最多回几条：10~15 之间，到点就结束话题
    peer_session_max: int = 12
    # 一场最多花多少：超了就用「省 token」的由头收场
    peer_session_budget: float = 0.07
    peer_session_gap: int = 600
    # 估算单价，只用来算预算，不参与记账（真账在 usage.db）
    peer_cost_per_reply: float = 0.0045
    # 最近这么久没有真人说话，同类的话就不接（防两只 bot 自己刷）
    peer_human_idle: int = 600
    # 「省 token」的由头多久最多说一次，免得每句都念叨
    peer_exit_cooldown: int = 1800
    # 主动找同类（肥鱼）搭话：默认开；聊天里「关闭找肥鱼 / 打开找肥鱼」也能关。
    enable_fish_chat: bool = True
    # 每群每小时最多主动找几次；两次之间、以及同一条鱼之间至少隔多久
    peer_outreach_max: int = 3
    peer_outreach_window: int = 3600
    peer_outreach_gap: int = 180
    peer_outreach_peer_gap: int = 600
    # ---- 旧对话链参数（P1 后不再使用，保留只为兼容旧 .env）----
    proactive_chain_seconds: int = 300
    proactive_chain_max: int = 12
    proactive_chain_gap: int = 5
    proactive_chain_strike_max: int = 2
    proactive_chain_turns: int = 24
    proactive_chain_hard_seconds: int = 900
    proactive_chain_member_seconds: int = 120
    proactive_chain_followup_seconds: int = 20
    proactive_chain_join_topic: bool = False
    # 她突然被叫到时，补多少群聊上下文（最近几条、只取多久以内的）
    group_context_lines: int = 8
    group_context_seconds: int = 600
    # 主动私信：她偶尔先开口找你
    enable_proactive_dm: bool = False
    dm_wake_min: int = 15
    dm_wake_max: int = 30
    dm_idle_minutes: int = 30
    dm_cooldown_minutes: int = 120
    dm_quiet_hours: str = "00:00-07:00"
    dm_daily_cap: int = 3
    dm_dry_run: bool = False
    # 上次主动提的要求没回，隔这么久可以再提一次（0 = 永不追问）
    dm_followup_hours: float = 0.0
    dm_state_file: str = "data/dm_state.json"
    # 有事真的私聊主人（要拍板的事 / 关于主人的消息 / 替人转述）：
    # 记着"问过什么、还没回"，主人回话时好把前因后果递回去
    owner_ask_file: str = "data/owner_ask.json"
    # 花销统计（单价：元 / 百万 token）
    enable_usage: bool = True
    usage_db: str = "data/usage.db"
    # 单价：元 / 百万 token，填**空闲时段**的价；高峰按倍率乘
    price_input: float = 1.0
    price_output: float = 4.0
    price_cached: float = 0.02
    price_peak_multiplier: float = 2.0
    # 联网搜索 / 抓网页：元 / 次成功请求（官方 5 元 / 1000 次）
    price_search_call: float = 0.005
    price_fetch_call: float = 0.005
    # 账户余额观测：每 N 分钟调一次 /user/balance，按余额下降记「账户实扣」。
    # 账户口径含 DSH/其他客户端，不等于本机机器人花的钱；两个口径分开显示。
    enable_balance_watch: bool = True
    balance_poll_minutes: int = 10
    # 留空 = DEEPSEEK_BASE_URL + /user/balance；中转站不支持就填自己的地址
    balance_api_url: str = ""
    # 主人私聊里每轮告诉她「今天本机花了多少、账户还剩多少」。
    # 只进动态上下文（不破前缀缓存）；群聊、别人私聊一律不带。
    money_awareness: bool = True
    # 每日花销日报：每天中午把昨天的账本私聊发给主人
    enable_daily_report: bool = True
    daily_report_at: str = "12:00"
    # 错过整点之后多久内还补发（小时）；0 = 过了那一分钟就等明天
    daily_report_catchup_hours: float = 8.0
    daily_report_state_file: str = "data/daily_report_state.json"
    # 演练：只在日志里打印，不真发（改样式时用）
    daily_report_dry_run: bool = False
    # 她自己的日记：每天睡前把当天的事写一篇，只存在本地，只有主人私聊能翻
    enable_diary: bool = True
    diary_at: str = "23:30"
    # 错过写日记的时刻之后多久内还补写（小时）；0 = 过了那一分钟就等明天
    diary_catchup_hours: float = 12.0
    diary_db: str = "data/diary.db"
    # 写完要不要私聊告诉主人一声（只报标题，不贴全文）
    diary_notify: bool = True
    # 当天至少有多少条对话素材才值得写；不够就记一笔 skipped，不花模型钱
    diary_min_turns: int = 6
    # 一次最多给模型看多少条片段（多了会写成流水账）
    diary_max_lines: int = 50
    # 一条链最多问模型几次（防止长链无限烧钱）
    proactive_chain_judge_max: int = 18
    # 消抖：收到群消息后等这么久再看，把期间的消息合成一回合判断
    proactive_debounce: float = 2.5

    @classmethod
    def from_env(cls) -> "Config":
        persona = os.environ.get(
            "BOT_PERSONA",
            "你是一个群聊里的朋友，说话口语化、简短。不要用 Markdown 排版。",
        )
        # persona.md 存在就优先用它（多行人格描述，比塞进一行环境变量好维护）
        persona_file = ROOT / "persona.md"
        if persona_file.exists():
            try:
                text = persona_file.read_text(encoding="utf-8").strip()
                if text:
                    persona = text
            except OSError:
                pass
        prefix_raw = os.environ.get("TRIGGER_PREFIX", "")
        bili_life_start = max(0, min(23, _int("BILI_LIFE_WINDOW_START", 10)))
        bili_life_end = max(0, min(24, _int("BILI_LIFE_WINDOW_END", 23)))
        if bili_life_end <= bili_life_start:
            bili_life_end = min(24, bili_life_start + 1)
        bili_life_topics = tuple(
            part.strip()
            for part in os.environ.get("BILI_LIFE_TOPICS", "")
            .replace("，", ",")
            .replace("、", ",")
            .split(",")
            if part.strip()
        )[:8]
        bili_life_sessdata = os.environ.get("BILI_LIFE_SESSDATA", "").strip()
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            thinking=os.environ.get("DEEPSEEK_THINKING", "disabled").strip().lower(),
            dry_run=_bool("DEEPSEEK_DRY_RUN"),
            ws_url=os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001").strip(),
            token=os.environ.get("ONEBOT_TOKEN", "").strip(),
            bot_qq=os.environ.get("BOT_QQ", "").strip(),
            persona=persona.strip(),
            trigger_at=_bool("TRIGGER_AT", True),
            trigger_prefix=_list("TRIGGER_PREFIX") or ([prefix_raw.strip()] if prefix_raw.strip() else []),
            group_whitelist=_list("GROUP_WHITELIST"),
            robot_qqs=_list("ROBOT_QQS"),
            ai_qqs=_list("AI_QQS"),
            speakers_file=_file_setting("SPEAKERS_FILE", "data/speakers.json"),
            allow_private=_bool("ALLOW_PRIVATE", True),
            max_history=max(0, _int("MAX_HISTORY", 12)),
            rate_limit=_float("RATE_LIMIT_SECONDS", 3.0),
            stale_message_seconds=_int("STALE_MESSAGE_SECONDS", 120),
            enable_split_messages=_bool("ENABLE_SPLIT_MESSAGES", True),
            split_delay_min=_float("SPLIT_DELAY_MIN", 0.4),
            split_delay_max=_float("SPLIT_DELAY_MAX", 1.0),
            split_max_segments=_int("SPLIT_MAX_SEGMENTS", 3),
            enable_followup=_bool("ENABLE_FOLLOWUP", True),
            followup_delay_min=_int("FOLLOWUP_DELAY_MIN", 90),
            followup_delay_max=_int("FOLLOWUP_DELAY_MAX", 240),
            followup_cooldown_minutes=_int("FOLLOWUP_COOLDOWN_MINUTES", 30),
            max_reply_chars=_int("MAX_REPLY_CHARS", 600),
            enable_search=_bool("ENABLE_SEARCH", True),
            enable_search_gate=_bool("SEARCH_GATE", True),
            search_gate_hourly=max(0, _int("SEARCH_GATE_HOURLY", 60)),
            search_gate_daily=max(0, _int("SEARCH_GATE_DAILY", 300)),
            search_reuse_minutes=max(0.0, _float("SEARCH_REUSE_MINUTES", 30.0)),
            enable_bili_card=_bool("ENABLE_BILI_CARD", True),
            bili_timeout=max(0.5, _float("BILI_TIMEOUT", 4.0)),
            bili_cache_minutes=max(0, _int("BILI_CACHE_MINUTES", 60)),
            bili_card_max_chars=max(80, _int("BILI_CARD_MAX_CHARS", 200)),
            enable_bili_cover=_bool("ENABLE_BILI_COVER", True),
            bili_cover_max_mb=max(0.2, _float("BILI_COVER_MAX_MB", 1.5)),
            enable_bili_archive=_bool("ENABLE_BILI_ARCHIVE", True),
            bili_sessdata=os.environ.get("BILI_SESSDATA", "").strip(),
            bili_archive_dir=_file_setting("BILI_ARCHIVE_DIR", "data/bili"),
            bili_archive_days=max(1, _int("BILI_ARCHIVE_DAYS", 7)),
            bili_archive_max_chars=max(200, _int("BILI_ARCHIVE_MAX_CHARS", 700)),
            bili_sync_timeout=max(0.0, _float("BILI_SYNC_TIMEOUT", 12.0)),
            enable_bili_frames=_bool("ENABLE_BILI_FRAMES", False),
            bili_frames_timeout=max(10.0, _float("BILI_FRAMES_TIMEOUT", 60.0)),
            bili_frames_cells=max(4, min(16, _int("BILI_FRAMES_CELLS", 12))),
            enable_bili_frames_tool=_bool("ENABLE_BILI_FRAMES_TOOL", True),
            bili_frames_tool_daily=max(0, _int("BILI_FRAMES_TOOL_DAILY", 6)),
            bili_frames_tool_max_minutes=max(
                1, min(180, _int("BILI_FRAMES_TOOL_MAX_MINUTES", 40))
            ),
            enable_bili_zoom=_bool("ENABLE_BILI_ZOOM", True),
            bili_frames_keep_hours=max(0.0, _float("BILI_FRAMES_KEEP_HOURS", 24.0)),
            enable_bili_extras=_bool("ENABLE_BILI_EXTRAS", True),
            enable_bili_knowledge=_bool("ENABLE_BILI_KNOWLEDGE", True),
            bili_knowledge_days=max(1, _int("BILI_KNOWLEDGE_DAYS", 30)),
            bili_api_timeout=max(1.0, _float("BILI_API_TIMEOUT", 6.0)),
            bili_rate=max(0.0, _float("BILI_RATE_SECONDS", 1.0)),
            bili_archive_timeout=max(5.0, _float("BILI_ARCHIVE_TIMEOUT", 25.0)),
            enable_bili_live=_bool("ENABLE_BILI_LIVE", False),
            bili_live_poll_seconds=max(15, _int("BILI_LIVE_POLL_SECONDS", 60)),
            bili_live_max_danmaku=max(1, _int("BILI_LIVE_MAX_DANMAKU", 40)),
            bili_live_note_lines=max(1, _int("BILI_LIVE_NOTE_LINES", 8)),
            bili_live_timeout=max(2.0, _float("BILI_LIVE_TIMEOUT", 6.0)),
            enable_live_frames=_bool("ENABLE_LIVE_FRAMES", False),
            bili_live_frames_seconds=max(
                4.0, min(30.0, _float("BILI_LIVE_FRAMES_SECONDS", 12.0))
            ),
            bili_live_frames_interval=max(
                0.5, min(5.0, _float("BILI_LIVE_FRAMES_INTERVAL", 1.33))
            ),
            bili_live_buffer_enabled=_bool("ENABLE_LIVE_BUFFER", True),
            bili_live_buffer_seconds=max(
                30.0, min(600.0, _float("BILI_LIVE_BUFFER_SECONDS", 120.0))
            ),
            bili_live_buffer_interval=max(
                5.0, min(120.0, _float("BILI_LIVE_BUFFER_INTERVAL", 15.0))
            ),
            bili_live_peak_enabled=_bool("ENABLE_LIVE_PEAK", True),
            bili_live_peak_min=max(2, _int("BILI_LIVE_PEAK_MIN", 8)),
            bili_live_peak_cooldown=max(
                30.0, _float("BILI_LIVE_PEAK_COOLDOWN", 120.0)
            ),
            enable_bili_live_memes=_bool("ENABLE_BILI_LIVE_MEMES", True),
            enable_live_danmaku=_bool("ENABLE_LIVE_DANMAKU", False),
            bili_jct=os.environ.get("BILI_JCT", "").strip(),
            bili_live_danmaku_max_chars=max(
                4, min(40, _int("BILI_LIVE_DANMAKU_MAX_CHARS", 20))
            ),
            bili_live_danmaku_interval=max(
                20.0, _float("BILI_LIVE_DANMAKU_INTERVAL", 90.0)
            ),
            bili_live_danmaku_daily=max(0, _int("BILI_LIVE_DANMAKU_DAILY", 10)),
            bili_live_danmaku_sent_file=_file_setting(
                "BILI_LIVE_DANMAKU_SENT_FILE", "data/live_danmaku_sent.json"
            ),
            bili_live_db=_file_setting("BILI_LIVE_DB", "data/live_danmaku.db"),
            bili_live_raw_days=max(1, _int("BILI_LIVE_RAW_DAYS", 7)),
            bili_live_term_days=max(1, _int("BILI_LIVE_TERM_DAYS", 30)),
            bili_live_sample_rate=max(0.0, min(1.0, _float("BILI_LIVE_SAMPLE_RATE", 0.1))),
            bili_live_min_hits=max(1, _int("BILI_LIVE_MIN_HITS", 3)),
            bili_live_min_speakers=max(1, _int("BILI_LIVE_MIN_SPEAKERS", 2)),
            bili_live_history_db=_file_setting(
                "BILI_LIVE_HISTORY_DB", "data/live_history.db"
            ),
            bili_live_history_days=max(30, _int("BILI_LIVE_HISTORY_DAYS", 180)),
            enable_bili_life=_bool("ENABLE_BILI_LIFE", False) and bool(bili_life_sessdata),
            bili_life_sessdata=bili_life_sessdata,
            bili_life_jct=os.environ.get("BILI_LIFE_JCT", "").strip(),
            bili_life_dir=_file_setting("BILI_LIFE_DIR", "data/bili_life"),
            bili_life_window_start=bili_life_start,
            bili_life_window_end=bili_life_end,
            bili_life_daily_runs=max(1, _int("BILI_LIFE_DAILY_RUNS", 10)),
            bili_life_daily_picks=max(1, min(40, _int("BILI_LIFE_DAILY_PICKS", 30))),
            bili_life_max_archives=max(1, min(3, _int("BILI_LIFE_MAX_ARCHIVES", 3))),
            bili_life_searches_per_run=max(
                0, min(6, _int("BILI_LIFE_SEARCHES_PER_RUN", 3))
            ),
            bili_life_daily_searches=max(0, _int("BILI_LIFE_DAILY_SEARCHES", 30)),
            bili_life_min_gap_hours=max(1, _int("BILI_LIFE_MIN_GAP_HOURS", 1)),
            bili_life_bored_minutes=max(5, _int("BILI_LIFE_BORED_MINUTES", 45)),
            bili_life_vision=_bool("BILI_LIFE_VISION", False),
            bili_life_vision_daily=max(0, _int("BILI_LIFE_VISION_DAILY", 10)),
            bili_life_vision_max_videos=max(
                0, min(2, _int("BILI_LIFE_VISION_MAX_VIDEOS", 1))
            ),
            bili_life_vision_max_grids=max(
                1, min(3, _int("BILI_LIFE_VISION_MAX_GRIDS", 3))
            ),
            bili_life_min_views=max(0, _int("BILI_LIFE_MIN_VIEWS", 1000)),
            bili_life_max_duration=max(
                60, min(7200, _int("BILI_LIFE_MAX_DURATION", 1200))
            ),
            bili_life_vision_max_duration=max(
                60, min(7200, _int("BILI_LIFE_VISION_MAX_DURATION", 1200))
            ),
            bili_life_topics=bili_life_topics,
            bili_life_interactions=_bool("BILI_LIFE_INTERACTIONS", False),
            bili_life_comment=_bool("BILI_LIFE_COMMENT", False),
            bili_life_like_daily=max(0, _int("BILI_LIFE_LIKE_DAILY", 18)),
            bili_life_collect_daily=max(0, _int("BILI_LIFE_COLLECT_DAILY", 9)),
            bili_life_follow_daily=max(0, _int("BILI_LIFE_FOLLOW_DAILY", 3)),
            bili_life_comment_daily=max(0, _int("BILI_LIFE_COMMENT_DAILY", 4)),
            bili_life_tell_daily=max(0, _int("BILI_LIFE_TELL_DAILY", 1)),
            bili_life_fav_folder_id=max(0, _int("BILI_LIFE_FAV_FOLDER_ID", 0)),
            chris_mid=os.environ.get("CHRIS_MID", "10000000").strip(),
            chris_db=_file_setting("CHRIS_DB", "data/chris_comments.db"),
            chris_sessdata=os.environ.get("CHRIS_SESSDATA", "").strip(),
            chris_rate=max(0.5, _float("CHRIS_RATE_SECONDS", 1.5)),
            chris_stop_412=max(1, _int("CHRIS_STOP_412", 5)),
            enable_chris_feedback=_bool("ENABLE_CHRIS_FEEDBACK", True),
            chris_feedback_file=_file_setting(
                "CHRIS_FEEDBACK_FILE", "data/chris_feedback.json"
            ),
            enable_vision=_bool("ENABLE_VISION", True),
            enable_image_cache=_bool("ENABLE_IMAGE_CACHE", True),
            image_cache_db=_file_setting("IMAGE_CACHE_DB", "data/image_cache.db"),
            image_cache_limit=_int("IMAGE_CACHE_LIMIT", 2000),
            max_images=max(0, _int("MAX_IMAGES", 4)),
            max_image_mb=max(1.0, _float("MAX_IMAGE_MB", 8.0)),
            image_context_seconds=max(0.0, _float("IMAGE_CONTEXT_SECONDS", 60.0)),
            image_wait_seconds=max(0.0, _float("IMAGE_WAIT_SECONDS", 2.5)),
            enable_forward=_bool("ENABLE_FORWARD", True),
            forward_max_depth=max(0, _int("FORWARD_MAX_DEPTH", 3)),
            forward_max_nodes=max(1, _int("FORWARD_MAX_NODES", 200)),
            forward_max_chars=max(200, _int("FORWARD_MAX_CHARS", 8000)),
            forward_max_calls=max(1, _int("FORWARD_MAX_CALLS", 8)),
            forward_preview_chars=max(0, _int("FORWARD_PREVIEW_CHARS", 300)),
            owner_qq=os.environ.get("OWNER_QQ", "").strip(),
            enable_friend_accept=_bool("FRIEND_AUTO_ACCEPT", False),
            friend_daily_cap=max(0, _int("FRIEND_DAILY_CAP", 3)),
            friend_min_tier=max(0, min(4, _int("FRIEND_MIN_TIER", 2))),
            friend_delete_daily=max(0, _int("FRIEND_DELETE_DAILY", 1)),
            friend_file=_file_setting("FRIEND_FILE", "data/friends.json"),
            enable_auto_ban=_bool("AUTO_BAN_ENABLED", True),
            auto_ban_warnings=max(1, _int("AUTO_BAN_WARNINGS", 3)),
            auto_ban_strike_days=max(1, _int("AUTO_BAN_STRIKE_DAYS", 7)),
            auto_ban_vision=_bool("AUTO_BAN_VISION", True),
            auto_ban_max_images=max(1, _int("AUTO_BAN_MAX_IMAGES", 2)),
            friend_request_refusal=os.environ.get(
                "FRIEND_REQUEST_REFUSAL",
                "我们才刚认识嘛，再多聊两句吧。",
            ).strip(),
            enable_sticker=_bool("ENABLE_STICKER", True),
            enable_sticker_index=_bool("ENABLE_STICKER_INDEX", True),
            sticker_candidates=max(0, min(12, _int("STICKER_CANDIDATES", 6))),
            enable_sticker_steal=_bool("ENABLE_STICKER_STEAL", True),
            sticker_steal_daily=max(0, _int("STICKER_STEAL_DAILY", 3)),
            sticker_steal_min_minutes=max(0.0, _float("STICKER_STEAL_MIN_MINUTES", 60.0)),
            sticker_steal_max_mb=max(0.5, _float("STICKER_STEAL_MAX_MB", 3.0)),
            sticker_steal_manual_max_mb=max(
                0.5, _float("STICKER_STEAL_MANUAL_MAX_MB", 10.0)
            ),
            sticker_steal_checks=max(0, _int("STICKER_STEAL_CHECKS", 30)),
            sticker_steal_color_filter=_bool("STICKER_STEAL_COLOR_FILTER", True),
            enable_sticker_replace=_bool("ENABLE_STICKER_REPLACE", True),
            sticker_replace_daily=max(0, _int("STICKER_REPLACE_DAILY", 3)),
            sticker_replace_max_mb=max(0.5, _float("STICKER_REPLACE_MAX_MB", 3.0)),
            enable_mood=_bool("ENABLE_MOOD", True),
            mood_file=_file_setting("MOOD_FILE", "data/mood.json"),
            mood_repeat_delta=_float("MOOD_REPEAT_DELTA", 22.0),
            mood_trigger_delta=_float("MOOD_TRIGGER_DELTA", 32.0),
            mood_decay_per_min=_float("MOOD_DECAY_PER_MIN", 2.5),
            mood_window_minutes=max(1, _int("MOOD_WINDOW_MINUTES", 15)),
            mood_silence_chance=max(
                0.0, min(1.0, _float("MOOD_SILENCE_CHANCE", 0.5))
            ),
            mood_trigger_words=_list("MOOD_TRIGGER_WORDS")
            or [
                "人机",
                "机器人",
                "复读机",
                "AI味",
                "ai味",
                "换模型",
                "什么模型",
                "什么版本",
                "掉线",
                "你回来了",
                "终于活了",
            ],
            familiar_db=_file_setting("FAMILIAR_DB", "data/familiar.db"),
            familiar_cache_seconds=max(1, _int("FAMILIAR_CACHE_SECONDS", 60)),
            familiar_stale_days=max(1, _int("FAMILIAR_STALE_DAYS", 30)),
            enable_familiar_note=_bool("ENABLE_FAMILIAR_NOTE", True),
            enable_affection=_bool("ENABLE_AFFECTION", True),
            affection_file=_file_setting("AFFECTION_FILE", "data/affection.json"),
            affection_free_gain=max(0, _int("AFFECTION_FREE_GAIN", 5)),
            affection_free_loss=max(0, _int("AFFECTION_FREE_LOSS", 10)),
            affection_fatigue_step=max(1, _int("AFFECTION_FATIGUE_STEP", 3)),
            affection_fatigue_factor=max(
                0.01, min(1.0, _float("AFFECTION_FATIGUE_FACTOR", 0.5))
            ),
            affection_min_multiplier=max(
                0.0, min(1.0, _float("AFFECTION_MIN_MULTIPLIER", 0.05))
            ),
            affection_score_max=max(1, _int("AFFECTION_SCORE_MAX", 30)),
            affection_decay_days=max(1, _int("AFFECTION_DECAY_DAYS", 14)),
            enable_memory=_bool("ENABLE_MEMORY", True),
            memory_db=_file_setting("MEMORY_DB", "data/memory.db"),
            memory_scope=(os.environ.get("MEMORY_SCOPE", "user").strip().lower() or "user"),
            enable_group_log=_bool("ENABLE_GROUP_LOG", True),
            group_log_db=_file_setting("GROUP_LOG_DB", "data/group_log.db"),
            roster_db=_file_setting("ROSTER_DB", "data/roster.db"),
            roster_ttl_seconds=max(300, _int("ROSTER_TTL_SECONDS", 1800)),
            member_state_file=_file_setting(
                "MEMBER_STATE_FILE", "data/member_state.json"
            ),
            enable_at=_bool("ENABLE_AT", False),
            enable_at_model_choice=_bool("ENABLE_AT_MODEL_CHOICE", True),
            at_state_file=_file_setting("AT_STATE_FILE", "data/at_state.json"),
            at_group_cooldown=max(0, _int("AT_GROUP_COOLDOWN", 300)),
            at_group_hourly_cap=max(0, _int("AT_GROUP_HOURLY_CAP", 3)),
            at_person_cooldown=max(0, _int("AT_PERSON_COOLDOWN", 1800)),
            at_person_daily_cap=max(0, _int("AT_PERSON_DAILY_CAP", 8)),
            at_present_seconds=max(0, _int("AT_PRESENT_SECONDS", 300)),
            at_miss_ignore_hours=max(0.0, _float("AT_MISS_IGNORE_HOURS", 24.0)),
            at_warmup_hours=max(0.0, _float("AT_WARMUP_HOURS", 6.0)),
            enable_style_learning=_bool("ENABLE_STYLE_LEARNING", False),
            enable_group_style=_bool("ENABLE_GROUP_STYLE", False),
            group_style_file=_file_setting("GROUP_STYLE_FILE", "data/group_style.json"),
            group_style_refresh_minutes=max(
                5, _int("GROUP_STYLE_REFRESH_MINUTES", 30)
            ),
            group_style_sample=max(50, _int("GROUP_STYLE_SAMPLE", 400)),
            group_style_min_sample=max(10, _int("GROUP_STYLE_MIN_SAMPLE", 30)),
            group_style_max_chars=max(80, _int("GROUP_STYLE_MAX_CHARS", 300)),
            group_style_adopt=os.environ.get("GROUP_STYLE_ADOPT", "观察为主").strip()
            or "观察为主",
            group_style_include_words=_bool("GROUP_STYLE_INCLUDE_WORDS", True),
            enable_corpus_recall=_bool("ENABLE_CORPUS_RECALL", False),
            corpus_index_db=_file_setting("CORPUS_INDEX_DB", "data/corpus_index.db"),
            corpus_embed_model=os.environ.get("CORPUS_EMBED_MODEL", "").strip(),
            corpus_recall_limit=max(1, _int("CORPUS_RECALL_LIMIT", 3)),
            corpus_recall_days=max(0.1, _float("CORPUS_RECALL_DAYS", 30.0)),
            corpus_recall_max_chars=max(80, _int("CORPUS_RECALL_MAX_CHARS", 260)),
            corpus_recall_min_score=max(
                0.0, min(1.0, _float("CORPUS_RECALL_MIN_SCORE", 0.36))
            ),
            corpus_recall_semantic_only=max(
                0.0, min(1.0, _float("CORPUS_RECALL_SEMANTIC_ONLY", 0.68))
            ),
            corpus_recall_exclude_minutes=max(
                0, _int("CORPUS_RECALL_EXCLUDE_MINUTES", 10)
            ),
            enable_corpus_sync=_bool("ENABLE_CORPUS_SYNC", True),
            corpus_sync_on_start=_bool("CORPUS_SYNC_ON_START", True),
            corpus_sync_minutes=max(0, _int("CORPUS_SYNC_MINUTES", 15)),
            corpus_terms_refresh_on_start=_bool(
                "CORPUS_TERMS_REFRESH_ON_START", True
            ),
            enable_hot_trends=_bool("ENABLE_HOT_TRENDS", True),
            hot_trends_file=_file_setting("HOT_TRENDS_FILE", "data/hot_trends.json"),
            hot_trends_hour=max(0, min(23, _int("HOT_TRENDS_HOUR", 6))),
            hot_trends_check_minutes=max(1, _int("HOT_TRENDS_CHECK_MINUTES", 15)),
            hot_trends_keep_days=max(1, _int("HOT_TRENDS_KEEP_DAYS", 7)),
            hot_trends_max_topics=max(20, _int("HOT_TRENDS_MAX_TOPICS", 300)),
            hot_trends_max_chars=max(100, _int("HOT_TRENDS_MAX_CHARS", 480)),
            hot_trends_trigger_words=_list("HOT_TRENDS_TRIGGER_WORDS")
            or [
                "热搜",
                "热梗",
                "什么梗",
                "最近火",
                "最近流行",
                "最近的梗",
                "网上都在说",
                "新鲜事",
                "新梗",
                "吃瓜",
                "出什么事了",
                "发生什么了",
                "out了",
            ],
            enable_trends_explain=_bool("ENABLE_TRENDS_EXPLAIN", True),
            trends_explain_count=max(1, min(20, _int("TRENDS_EXPLAIN_COUNT", 8))),
            enable_repeat_along=_bool("ENABLE_REPEAT_ALONG", True),
            repeat_along_min_people=max(2, _int("REPEAT_ALONG_MIN_PEOPLE", 3)),
            repeat_along_min_repeats=max(2, _int("REPEAT_ALONG_MIN_REPEATS", 3)),
            repeat_along_window_seconds=max(
                1.0, _float("REPEAT_ALONG_WINDOW_SECONDS", 30.0)
            ),
            repeat_along_cooldown_seconds=max(
                0, _int("REPEAT_ALONG_COOLDOWN_SECONDS", 300)
            ),
            repeat_along_max_chars=max(1, _int("REPEAT_ALONG_MAX_CHARS", 24)),
            repeat_along_per_hour=max(0, _int("REPEAT_ALONG_PER_HOUR", 6)),
            enable_memes=_bool("ENABLE_MEMES", True),
            enable_chat_meme_capture=_bool("ENABLE_CHAT_MEME_CAPTURE", True),
            memes_file=_file_setting("MEMES_FILE", "data/memes.json"),
            memes_keep_days=max(1, _int("MEMES_KEEP_DAYS", 30)),
            memes_max_per_group=max(5, _int("MEMES_MAX_PER_GROUP", 60)),
            memes_max_chars=max(120, _int("MEMES_MAX_CHARS", 360)),
            enable_meme_verify=_bool("ENABLE_MEME_VERIFY", True),
            meme_verify_per_day=max(0, _int("MEME_VERIFY_PER_DAY", 2)),
            meme_verify_per_run=max(1, _int("MEME_VERIFY_PER_RUN", 1)),
            meme_verify_count=max(3, min(20, _int("MEME_VERIFY_COUNT", 8))),
            meme_verify_min_speakers=max(3, _int("MEME_VERIFY_MIN_SPEAKERS", 5)),
            meme_verify_min_hits=max(3, _int("MEME_VERIFY_MIN_HITS", 8)),
            meme_verify_min_days=max(2, _int("MEME_VERIFY_MIN_DAYS", 3)),
            meme_intake_file=_file_setting(
                "MEME_INTAKE_FILE", "data/meme_intake.json"
            ),
            meme_intake_ttl_minutes=max(5, _int("MEME_INTAKE_TTL_MINUTES", 120)),
            enable_meme_ask=_bool("ENABLE_MEME_ASK", True),
            meme_ask_file=_file_setting("MEME_ASK_FILE", "data/meme_asks.json"),
            meme_ask_ttl_minutes=max(5, _int("MEME_ASK_TTL_MINUTES", 120)),
            meme_ask_cooldown_minutes=max(0, _int("MEME_ASK_COOLDOWN_MINUTES", 10)),
            meme_ask_per_day=max(0, _int("MEME_ASK_PER_DAY", 5)),
            memes_web_keep_days=max(1, _int("MEMES_WEB_KEEP_DAYS", 365)),
            meme_aliases_file=_file_setting(
                "MEME_ALIASES_FILE", "data/meme_aliases.json"
            ),
            enable_meme_wiki=_bool("ENABLE_MEME_WIKI", True),
            meme_wiki_db=_file_setting("MEME_WIKI_DB", "data/meme_wiki.db"),
            meme_wiki_max_chars=max(120, _int("MEME_WIKI_MAX_CHARS", 220)),
            meme_wiki_mention=_bool("MEME_WIKI_MENTION", True),
            meme_wiki_cooldown_minutes=max(
                0, _int("MEME_WIKI_COOLDOWN_MINUTES", 10)
            ),
            enable_pricetime_tier=_bool("ENABLE_PRICETIME_TIER", True),
            pricetime_peak_tier=_int("PRICETIME_PEAK_TIER", 0),
            pricetime_offpeak_tier=_int("PRICETIME_OFFPEAK_TIER", 1),
            pricetime_peak_windows=os.environ.get(
                "PRICETIME_PEAK_WINDOWS", "09:00-12:00,14:00-18:00"
            ).strip(),
            pricetime_holidays=os.environ.get("PRICETIME_HOLIDAYS", "").strip(),
            forget_rate=_float("MEMORY_FORGET_RATE", 0.08),
            forget_threshold=_float("MEMORY_FORGET_THRESHOLD", 0.15),
            promote_hits=_int("MEMORY_PROMOTE_HITS", 3),
            promote_min_gap_hours=_float("MEMORY_PROMOTE_MIN_GAP_HOURS", 12.0),
            promote_min_score=_float("MEMORY_PROMOTE_MIN_SCORE", 0.5),
            promote_min_chars=max(1, _int("MEMORY_PROMOTE_MIN_CHARS", 12)),
            recall_limit=max(0, _int("RECALL_LIMIT", 5)),
            recall_semantic_floor=max(0, _int("RECALL_SEMANTIC_FLOOR", 1)),
            recall_min_score=float(_int("RECALL_MIN_SCORE", 25)) / 100,
            recall_self_max=max(0, _int("RECALL_SELF_MAX", 1)),
            memory_question_recall=max(0, _int("MEMORY_QUESTION_RECALL", 6)),
            memory_question_turn_limit=max(
                0, _int("MEMORY_QUESTION_TURN_LIMIT", 48)
            ),
            memory_question_context_lines=max(
                0, _int("MEMORY_QUESTION_CONTEXT_LINES", 20)
            ),
            memory_group_log_days=max(0, _int("MEMORY_GROUP_LOG_DAYS", 7)),
            memory_group_log_limit=max(0, _int("MEMORY_GROUP_LOG_LIMIT", 6)),
            memory_status_note=_bool("MEMORY_STATUS_NOTE", True),
            enable_memory_tool=_bool("ENABLE_MEMORY_TOOL", True),
            knowledge_ttl_days=max(0, _int("KNOWLEDGE_TTL_DAYS", 7)),
            enable_semantic=_bool("ENABLE_SEMANTIC", True),
            embed_model=os.environ.get("EMBED_MODEL", "models/bge-small-zh-v1.5").strip(),
            semantic_z=_float("SEMANTIC_Z", 1.4),
            semantic_cos=_float("SEMANTIC_COS", 0.35),
            semantic_candidates=max(100, _int("SEMANTIC_CANDIDATES", 3000)),
            semantic_recall_bump=max(0.0, _float("SEMANTIC_RECALL_BUMP", 0.05)),
            enable_concession=_bool("ENABLE_CONCESSION", True),
            concession_file=_file_setting("CONCESSION_FILE", "data/concession.json"),
            concession_ttl_seconds=max(60, _int("CONCESSION_TTL_SECONDS", 1800)),
            concession_min_gap_seconds=max(
                0, _int("CONCESSION_MIN_GAP_SECONDS", 15)
            ),
            concession_stage_seconds=max(0, _int("CONCESSION_STAGE_SECONDS", 20)),
            enable_qzone=_bool("ENABLE_QZONE", False),
            qzone_private=_int("QZONE_PRIVATE", 0),
            qzone_require_approval=_bool("QZONE_REQUIRE_APPROVAL", True),
            qzone_daily_limit=_int("QZONE_DAILY_LIMIT", 2),
            qzone_cooldown_minutes=_int("QZONE_COOLDOWN_MINUTES", 60),
            qzone_quiet_hours=os.environ.get("QZONE_QUIET_HOURS", "00:00-08:00").strip(),
            qzone_limits_file=_file_setting("QZONE_LIMITS_FILE", "data/qzone_limits.json"),
            enable_qzone_auto=_bool("ENABLE_QZONE_AUTO", False),
            qzone_auto_window=os.environ.get("QZONE_AUTO_WINDOW", "18:00-23:00").strip(),
            qzone_auto_min_hours=max(1, _int("QZONE_AUTO_MIN_HOURS", 20)),
            qzone_auto_max_hours=max(1, _int("QZONE_AUTO_MAX_HOURS", 28)),
            qzone_auto_min_turns=max(1, _int("QZONE_AUTO_MIN_TURNS", 4)),
            qzone_auto_check_minutes=max(5, _int("QZONE_AUTO_CHECK_MINUTES", 15)),
            qzone_auto_state_file=_file_setting("QZONE_AUTO_STATE_FILE", "data/qzone_auto.json"),
            enable_qzone_reply=_bool("ENABLE_QZONE_REPLY", False),
            qzone_reply_check_minutes=max(5, _int("QZONE_REPLY_CHECK_MINUTES", 30)),
            qzone_reply_daily_limit=max(0, _int("QZONE_REPLY_DAILY_LIMIT", 3)),
            qzone_reply_cooldown_minutes=max(0, _int("QZONE_REPLY_COOLDOWN_MINUTES", 10)),
            qzone_reply_per_person_hours=max(
                0, _int("QZONE_REPLY_PER_PERSON_HOURS", 6)
            ),
            qzone_reply_max_posts=max(1, _int("QZONE_REPLY_MAX_POSTS", 2)),
            qzone_reply_max_age_hours=max(0, _int("QZONE_REPLY_MAX_AGE_HOURS", 48)),
            qzone_reply_quiet_hours=os.environ.get(
                "QZONE_REPLY_QUIET_HOURS", "00:00-08:00"
            ).strip(),
            qzone_reply_ignore_uins=os.environ.get("QZONE_REPLY_IGNORE_UINS", "").strip(),
            qzone_reply_state_file=_file_setting(
                "QZONE_REPLY_STATE_FILE", "data/qzone_reply.json"
            ),
            qzone_reply_notify_owner=_bool("QZONE_REPLY_NOTIFY_OWNER", False),
            enable_startup_greeting=_bool("ENABLE_STARTUP_GREETING", True),
            presence_dm_enabled=_bool("PRESENCE_DM_ENABLED", True),
            presence_dm_stable_seconds=max(0, _int("PRESENCE_DM_STABLE_SECONDS", 60)),
            presence_dm_min_downtime_minutes=max(
                0, _int("PRESENCE_DM_MIN_DOWNTIME_MINUTES", 30)
            ),
            presence_dm_cooldown_minutes=max(
                0, _int("PRESENCE_DM_COOLDOWN_MINUTES", 360)
            ),
            presence_dm_daily_cap=max(0, _int("PRESENCE_DM_DAILY_CAP", 1)),
            presence_dm_quiet_hours=os.environ.get(
                "PRESENCE_DM_QUIET_HOURS", "23:00-08:00"
            ).strip(),
            presence_dm_max_recipients=max(
                0, _int("PRESENCE_DM_MAX_RECIPIENTS", 20)
            ),
            presence_dm_state_file=_file_setting(
                "PRESENCE_DM_STATE_FILE", "data/presence_dm_state.json"
            ),
            enable_catchup_dm=_bool("ENABLE_CATCHUP_DM", True),
            catchup_max_age_hours=max(0.0, _float("CATCHUP_MAX_AGE_HOURS", 24.0)),
            catchup_settle_seconds=max(0.0, _float("CATCHUP_SETTLE_SECONDS", 20.0)),
            catchup_max_contacts=max(0, _int("CATCHUP_MAX_CONTACTS", 20)),
            catchup_history_count=max(1, _int("CATCHUP_HISTORY_COUNT", 100)),
            catchup_max_replies=max(0, _int("CATCHUP_MAX_REPLIES", 10)),
            catchup_reply_spacing_seconds=max(
                0.0, _float("CATCHUP_REPLY_SPACING_SECONDS", 2.0)
            ),
            catchup_apology_minutes=max(
                0.0, _float("CATCHUP_APOLOGY_MINUTES", 10.0)
            ),
            catchup_dry_run=_bool("CATCHUP_DRY_RUN", False),
            enable_proactive=_bool("ENABLE_PROACTIVE", False),
            enable_risk_mode=_bool("ENABLE_RISK_MODE", False),
            enable_saycheck=_bool("ENABLE_SAYCHECK", True),
            proactive_keywords=_list("PROACTIVE_KEYWORDS")
            or ["鲸鱼", "deepseek", "大肥鱼", "deepseek娘", "模型", "本鱼"],
            proactive_keyword_min_len=_int("PROACTIVE_KEYWORD_MIN_LEN", 2),
            proactive_chance=_float("PROACTIVE_CHANCE", 0.03),
            proactive_cooldown=_int("PROACTIVE_COOLDOWN", 300),
            quiet_minutes=_int("QUIET_MINUTES", 5),
            quiet_self_names=_list("QUIET_SELF_NAMES")
            or ["大肥鱼", "肥鱼", "鲸鱼", "鲸鱼娘"],
            merge_window_seconds=max(0.0, _float("MERGE_WINDOW_SECONDS", 0.8)),
            link_wait_seconds=max(0.0, _float("LINK_WAIT_SECONDS", 2.5)),
            merge_cap_seconds=max(0.0, _float("MERGE_CAP_SECONDS", 8.0)),
            proactive_merge_cap_seconds=max(
                0.0, _float("PROACTIVE_MERGE_CAP_SECONDS", 8.0)
            ),
            proactive_hourly_cap=_int("PROACTIVE_HOURLY_CAP", 6),
            proactive_followup_seconds=max(
                0, _int("PROACTIVE_FOLLOWUP_SECONDS", 90)
            ),
            proactive_followup_max_replies=max(
                0, _int("PROACTIVE_FOLLOWUP_MAX_REPLIES", 6)
            ),
            proactive_followup_immediate_seconds=max(
                0, _int("PROACTIVE_FOLLOWUP_IMMEDIATE_SECONDS", 30)
            ),
            proactive_followup_gap=max(0, _int("PROACTIVE_FOLLOWUP_GAP", 5)),
            peer_chat_enabled=_bool("PEER_CHAT_ENABLED", True),
            peer_pair_max=max(0, _int("PEER_PAIR_MAX", 12)),
            peer_pair_window=max(0, _int("PEER_PAIR_WINDOW", 600)),
            peer_group_max=max(0, _int("PEER_GROUP_MAX", 15)),
            peer_group_window=max(0, _int("PEER_GROUP_WINDOW", 3600)),
            peer_session_max=max(0, _int("PEER_SESSION_MAX", 12)),
            peer_session_budget=max(0.0, _float("PEER_SESSION_BUDGET", 0.07)),
            peer_session_gap=max(0, _int("PEER_SESSION_GAP", 600)),
            peer_cost_per_reply=max(0.0, _float("PEER_COST_PER_REPLY", 0.0045)),
            peer_human_idle=max(0, _int("PEER_HUMAN_IDLE", 600)),
            peer_exit_cooldown=max(0, _int("PEER_EXIT_COOLDOWN", 1800)),
            enable_fish_chat=_bool("ENABLE_FISH_CHAT", True),
            peer_outreach_max=max(0, _int("PEER_OUTREACH_MAX", 3)),
            peer_outreach_window=max(0, _int("PEER_OUTREACH_WINDOW", 3600)),
            peer_outreach_gap=max(0, _int("PEER_OUTREACH_GAP", 180)),
            peer_outreach_peer_gap=max(0, _int("PEER_OUTREACH_PEER_GAP", 600)),
            proactive_chain_seconds=_int("PROACTIVE_CHAIN_SECONDS", 300),
            proactive_chain_max=_int("PROACTIVE_CHAIN_MAX", 12),
            proactive_chain_gap=_int("PROACTIVE_CHAIN_GAP", 5),
            proactive_chain_strike_max=max(
                0, _int("PROACTIVE_CHAIN_STRIKE_MAX", 2)
            ),
            proactive_chain_turns=max(0, _int("PROACTIVE_CHAIN_TURNS", 24)),
            proactive_chain_hard_seconds=max(
                0, _int("PROACTIVE_CHAIN_HARD_SECONDS", 900)
            ),
            proactive_chain_member_seconds=max(
                0, _int("PROACTIVE_CHAIN_MEMBER_SECONDS", 120)
            ),
            proactive_chain_followup_seconds=max(
                0, _int("PROACTIVE_CHAIN_FOLLOWUP_SECONDS", 20)
            ),
            proactive_chain_join_topic=_bool("PROACTIVE_CHAIN_JOIN_TOPIC", False),
            group_context_lines=max(0, _int("GROUP_CONTEXT_LINES", 8)),
            group_context_seconds=max(0, _int("GROUP_CONTEXT_SECONDS", 600)),
            response_tier=_int("RESPONSE_TIER", -1),
            enable_proactive_dm=_bool("PROACTIVE_DM", False),
            dm_wake_min=_int("DM_WAKE_MIN", 15),
            dm_wake_max=_int("DM_WAKE_MAX", 30),
            dm_idle_minutes=_int("DM_IDLE_MINUTES", 30),
            dm_cooldown_minutes=_int("DM_COOLDOWN_MINUTES", 120),
            dm_quiet_hours=os.environ.get("DM_QUIET_HOURS", "00:00-07:00").strip(),
            dm_daily_cap=_int("DM_DAILY_CAP", 3),
            dm_dry_run=_bool("DM_DRY_RUN", False),
            dm_followup_hours=max(0.0, _float("DM_FOLLOWUP_HOURS", 0.0)),
            dm_state_file=_file_setting("DM_STATE_FILE", "data/dm_state.json"),
            owner_ask_file=_file_setting("OWNER_ASK_FILE", "data/owner_ask.json"),
            enable_usage=_bool("ENABLE_USAGE", True),
            usage_db=_file_setting("USAGE_DB", "data/usage.db"),
            price_input=_float("PRICE_INPUT", 1.0),
            price_output=_float("PRICE_OUTPUT", 4.0),
            price_cached=_float("PRICE_CACHED", 0.02),
            price_peak_multiplier=_float("PRICE_PEAK_MULTIPLIER", 2.0),
            price_search_call=_float("PRICE_SEARCH_CALL", 0.005),
            price_fetch_call=_float("PRICE_FETCH_CALL", 0.005),
            enable_balance_watch=_bool("ENABLE_BALANCE_WATCH", True),
            balance_poll_minutes=max(1, _int("BALANCE_POLL_MINUTES", 10)),
            balance_api_url=os.environ.get("BALANCE_API_URL", "").strip(),
            money_awareness=_bool("MONEY_AWARENESS", True),
            enable_daily_report=_bool("ENABLE_DAILY_REPORT", True),
            daily_report_at=os.environ.get("DAILY_REPORT_AT", "12:00").strip() or "12:00",
            daily_report_catchup_hours=max(
                0.0, _float("DAILY_REPORT_CATCHUP_HOURS", 8.0)
            ),
            daily_report_state_file=_file_setting("DAILY_REPORT_STATE_FILE", "data/daily_report_state.json"),
            daily_report_dry_run=_bool("DAILY_REPORT_DRY_RUN", False),
            enable_diary=_bool("ENABLE_DIARY", True),
            diary_at=os.environ.get("DIARY_AT", "23:30").strip() or "23:30",
            diary_catchup_hours=max(0.0, _float("DIARY_CATCHUP_HOURS", 12.0)),
            diary_db=_file_setting("DIARY_DB", "data/diary.db"),
            diary_notify=_bool("DIARY_NOTIFY", True),
            diary_min_turns=max(0, _int("DIARY_MIN_TURNS", 6)),
            diary_max_lines=max(4, _int("DIARY_MAX_LINES", 50)),
            proactive_chain_judge_max=_int("PROACTIVE_CHAIN_JUDGE_MAX", 18),
            proactive_debounce=max(0.0, _float("PROACTIVE_DEBOUNCE", 2.5)),
        )

    def validate(self) -> list[str]:
        problems = []
        placeholder = not self.api_key or "REPLACE" in self.api_key.upper() or "换成" in self.api_key
        if not self.dry_run and placeholder:
            problems.append(
                "DEEPSEEK_API_KEY 还没填（想先不花钱测链路就把 DEEPSEEK_DRY_RUN 改成 1）"
            )
        if not self.bot_qq:
            problems.append("BOT_QQ 没填（机器人自己的 QQ 号，不然群聊里判断不了有没有 @ 它）")
        if not self.ws_url.startswith("ws"):
            problems.append(f"ONEBOT_WS_URL 应该以 ws:// 开头，现在是 {self.ws_url!r}")
        return problems
