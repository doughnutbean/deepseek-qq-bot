# deepseek-qq-bot

> 把 DeepSeek 接进 QQ 群的一套完整实现。不是「收到消息就转发给模型」的 demo，而是一个真在群里跑了几个月的搭子：有长期记忆、会记群里的说法、会发表情包、会陪看直播、会自己算今天花了多少钱。

```
QQ 客户端（小号） ── SnowLuma ── OneBot 11 ── run.py ── DeepSeek API
                                    ↑
                            判断该不该回 / 调什么工具 / 记什么 / 花多少
```

**人设是示例的。** `persona.md` 里那位「小D」只是占位，换成你自己的，它才是你的。

---

## 它现在会什么

| 能力 | 怎么做的 | 主要代码 |
|---|---|---|
| 群聊 / 私聊 | @ 或前缀触发，限流、去重、自己回自己会拦住 | `app.py` `atgate.py` `flowgate.py` |
| 长期记忆 | SQLite 存「事实」而不是原话，按相似度召回，过期的会自己淡出 | `memory.py` `facts.py` `forget.py` `embed.py` |
| 人设分层 | 人设按段切成常驻 / 按需 / 只对群或只对私聊，省 token | `persona_layers.py` `promptbudget.py` |
| 本地指令 | 「多说点 / 少说话 / 关闭热梗」这类调参走纯本地规则，**0 token**，不劳模型 | `commands.py` `prefs.py` |
| 工具调用 | 搜索、抓网页、看图、查 B 站、发表情、记梗、改签名，模型自己决定调不调 | `tool_spec.py` `tools.py` `*_tools.py` |
| 群梗 | 群里长出来的黑话：收集 → 分级 → 验证 → 谐音代称 → 注入 | `memes.py` `meme_schema.py` `meme_intake.py` `memewiki.py` |
| 表情包 | 收图、审图（视觉模型过一遍）、按语境挑、被换掉会记账 | `stickers.py` `stickeranno.py` `stickereplace.py` |
| B 站 | 分享卡片解析成标题 + 简介；视频抽帧理解；链接不会整条被丢掉 | `linkcard.py` `bili.py` `bili_frames.py` |
| 直播陪看 | 弹幕高频词、场面帧、直播梗单独入库，不跟群梗混在一起 | `bili_live.py` `live_danmaku.py` `live_frames.py` |
| 主动一点 | 掉线补偿、久没说话会找你、心情和好感度会随对话变 | `proactive.py` `presence.py` `catchup.py` `mood.py` `affection.py` |
| 跟人学说话 | 从语料里蒸馏某个固定对象的说话口吻，带审校和反馈闭环 | `chris_crawler.py` `chris_distill.py` `chris_judge.py` |
| 花钱可见 | 每次请求都记账（token / 搜索 / 图形），余额对账，每天中午发日报 | `usage.py` `usagebook.py` `balance.py` `dailyreport.py` |
| 群管 | 好友申请、群邀请、退群、免打扰、群友只读自查 | `friends.py` `groupinvites.py` `memberperms.py` `memberhelp.py` |
| 说话安全 | 出稿前过一遍口癖/复读/敏感词检查，不干净就不发 | `saycheck.py` `antislop.py` `stopwords.py` |

---

## 它是怎么搭起来的

三个角色，缺一不可：

| 角色 | 是什么 | 谁来做 |
|---|---|---|
| **协议端** | 冒充 QQ 客户端收发消息，翻译成标准协议 | [SnowLuma](https://github.com/SnowLuma) 2.x（Docker 里跑 Linux QQ） |
| **协议** | 一套约定好的 JSON 格式（收事件 / 发动作） | OneBot 11 |
| **业务逻辑** | 判断该不该回、调模型、发回去 | 本仓库的 `run.py` |

QQ 没给普通人开放个人号接口。协议端通过注入 QQNT 客户端，把「收消息 / 发消息」暴露成
OneBot 11 的 WebSocket —— 这是目前最主流的做法，代价是**违反 QQ 用户协议，有封号风险**（见文末）。

线上用的是 **SnowLuma 2.x**。更早用的是 NapCat：本机被连续打「设备存在外挂」之后，
整套搬到服务器，协议端也换成了 SnowLuma，现在不再用 NapCat。

---

## 快速开始

### 0. 你需要

- Python 3.10+
- 一个 **QQ 小号**（新号先养几天，别一注册就挂机器人）
- 一个 DeepSeek API Key：在 [platform.deepseek.com](https://platform.deepseek.com/api_keys) 创建，充 10 块钱够玩很久
- 一台常开的机器（自己电脑也行，关机就掉线）

### 1. 起协议端（SnowLuma）

线上的形态是：**一个 Docker 容器里同时跑 Linux QQ 和 SnowLuma**，对外只暴露一个
OneBot WebSocket。部署方式看 SnowLuma 自己的文档，跑起来之后容器里会开这几个口：

| 端口 | 干什么 |
|---|---|
| 3001 | OneBot WebSocket，机器人连这个 |
| 5099 | WebUI，配 OneBot、看日志（只听 127.0.0.1） |
| 6081 | noVNC，第一次扫码登录用（用完就关） |

第一次登录：打开 noVNC（或 WebUI 里的二维码），用**小号**扫一次。登录态存在容器卷里，
之后重启容器会自动登录，不用反复扫。

### 2. 开 OneBot 接口（关键一步，很多人卡在这）

在 WebUI 里新建一个 **WebSocket 服务端**（有的版本叫「网络配置」）：

| 配置项 | 填什么 |
|---|---|
| 端口 | `3001` |
| Host | `0.0.0.0` |
| 消息格式 | `array` ← **必须是 array**，`string` 是 CQ 码老格式 |
| Token | 本机自用可留空；**公网部署必须设** |
| 启用 | 勾上「保存时启用」 |

保存后，浏览器访问 `http://127.0.0.1:3001` 能看到响应就说明通了。

改完 OneBot 配置只需要重启协议端这一层，**QQ 不会掉线**：

```bash
docker exec snowluma supervisorctl restart snowluma
```

> **为什么是「服务端」？** 因为要**你的程序**去连协议端。反过来（协议端当客户端连你）
> 叫「反向 WS」，适合机器人跑在云上、协议端跑在本机的情况。本仓库按正向 WS 写的，
> 想反过来就改 `.env` 里的 `ONEBOT_WS_URL`，代码不用动。
>
> **换别的 OneBot 11 实现也能接**：正向 WS、`array` 格式、端口对得上就行。

### 3. 配 `.env`

```powershell
Copy-Item .env.example .env
notepad .env      # 填 DEEPSEEK_API_KEY 和 BOT_QQ，就这两项必填
```

### 4. 跑起来

```powershell
python -m pip install -r requirements.txt
python run.py
```

看到这两行就是通了：

```
INFO qqbot | 模型 deepseek-flash @ https://api.deepseek.com（思考模式: disabled）
INFO qqbot.onebot | 已连上 OneBot: ws://127.0.0.1:3001
```

私聊直接回；群里默认要 `@小D` 才回（防刷屏，`TRIGGER_AT` / `TRIGGER_PREFIX` 可改）。

### 5. 没 Key 也能先验链路

```powershell
# 终端 1：假协议端，把收到的消息打印出来
python tools/mock_napcat.py

# 终端 2：不调真模型，只回显
$env:DEEPSEEK_DRY_RUN='1'; $env:BOT_QQ='10001'; python run.py
```

终端 1 里看到回复内容，说明事件解析、触发判断、发送这条链路全通了，剩下只用查 QQ 那一侧。

其他自检：

```powershell
python tools/selftest.py        # 消息解析 / Markdown 清理 / 长文切分
python tools/prompt_budget.py   # 看每次请求的固定前缀花在哪（人设通常占七成）
python tools/persona_layers.py  # 看人设每段的字符数和分层结果
```

---

## 配置

`.env.example` 里有 391 项，每项都有注释。常用的就这几个：

| 变量 | 作用 |
|---|---|
| `DEEPSEEK_MODEL` | 默认 `deepseek-flash`；复杂场景可换更强的 |
| `DEEPSEEK_THINKING` | 默认 `disabled`，QQ 聊天开思考又慢又贵 |
| `MAX_HISTORY` | 带几轮上下文，默认 12；越长每轮越贵 |
| `TRIGGER_AT` / `TRIGGER_PREFIX` | 群里怎么算「叫她」 |
| `GROUP_WHITELIST` | 只在这些群生效，留空 = 所有群 |
| `ENABLE_SEARCH` / `ENABLE_VISION` | 联网搜索 / 看图，按需开 |
| `ENABLE_DAILY_REPORT` | 每天中午把昨天的账私聊发给主人 |

文件类配置写裸文件名会自动落到 `data/`，写绝对路径就按绝对路径。

---

## 项目结构

```
deepseek-qq-bot/
├── run.py                  入口：读配置、起连接、挂后台任务
├── persona.md              人设（示例，换成你自己的）
├── persona.layers.json     哪一段常驻、哪一段按需
├── .env.example            391 项配置，带注释
├── qqbot/                  业务逻辑
│   ├── app.py              触发判断、限流、上下文、发送（主循环）
│   ├── config.py           .env 解析 + 启动校验
│   ├── onebot.py           OneBot 11 正向 WS 客户端（含自动重连）
│   ├── llm.py              DeepSeek 调用 + Markdown 清理 + 长文切分
│   ├── commands.py         本地指令，纯规则 0 token
│   ├── memory.py           长期记忆 + 召回
│   ├── memes.py            群梗
│   ├── stickers.py         表情包
│   ├── bili*.py            B 站：卡片、抽帧、直播
│   ├── live_*.py           直播陪看
│   ├── *_tools.py          给模型的 function calling 工具
│   └── caps/ domains/      能力表、领域模块
├── tools/                  自检与联调脚本
└── docs/                   设计文档
```

想改行为，90% 的情况只动 `app.py` 里的 `on_event`；想加指令动 `commands.py`，
先看 `docs/指令规范.md`；想给人设动 `persona.md`，先看 `docs/人设分层.md`。

---

## 几个设计上的取舍

**1. 调参指令不走模型。** 「多说点」「少说话」「关闭热梗」这类上百种说法，全在
`commands.py` 里做整句匹配（连否定词都处理），识别一次 0 token、0 延迟。交给模型判断，
既慢又贵，还会时灵时不灵。

**2. 人设分层，不是一直全塞。** 44 KB 的人设整份塞进去，光前缀就上万 token。
分层之后常驻的只有核心几段，「来历」「被调戏」「自我介绍」这类只在相关话题被提起时才注入。
`persona.layers.json` 决定谁常驻，`promptbudget.py` 告诉你省了多少。

**3. 记忆记「事实」，不记原话。** 存的是「主人养了只猫叫土豆」这种结构化事实，
带时间、来源、置信度，会过期、会被新信息覆盖。原话既不存也不复述。

**4. 花钱要看得见。** 令牌、联网搜索、看图分别计价，每次请求单独记一行，
按每次请求自己的开始时间取峰谷价。日报会把「本机花的」和「账户余额掉的」分开写，
两个口径不混。

**5. 隐私是硬边界。** 身份证、手机号、住址、银行卡这类字段在入库和出稿两个环节都被正则拦住
（`facts.py` `concession.py` `memory_tools.py`），不是靠提示词求它别说。

---

## 已知边界

- **个人号路线违反 QQ 用户协议，有封号风险。** 自己玩、小群自用可以，**别拿常用大号试**。
  想长期稳定就转 QQ 开放平台，业务逻辑那层（`app.py`）基本可以原样保留。
- **公网部署必须给协议端设 Token**，否则等于把发消息的权限挂在公网上。
- **别把群聊数据提交进仓库。** `.gitignore` 挡住了 `data/`、日志和数据库，
  但你自己新加的文件要自己看一眼：群号、QQ 号、聊天记录都不该进公开仓库。

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [指令规范](docs/指令规范.md) | 调参指令怎么设计：成对、整句匹配、否定词 |
| [人设分层](docs/人设分层.md) | 常驻 / 按需 / 只对群，怎么切、怎么回滚 |
| [示例规范](docs/示例规范.md) | 人设文档里怎么写「遇到什么情况怎么回」 |
| [Token 预算](docs/Token预算.md) | 一次请求的钱花在哪，怎么省 |
| [工具规范](docs/工具规范.md) | 给模型加工具时的 schema、描述、权限怎么写 |
| [能力表工作流](docs/能力表工作流.md) | 新增用户能感知的功能，怎么登记进能力表 |
| [Git 提交规范](docs/Git提交规范.md) | 一次提交只装自己改的文件 |

---

## License

[MIT](LICENSE)
