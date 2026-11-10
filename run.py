"""入口：python run.py"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
import logging
import os
import sys

from qqbot import presence
from qqbot.app import Bot
from qqbot.catchup import watch as catchup_watch
from qqbot.config import DATA_DIR, ROOT, Config, load_env
from qqbot.farewell import EXIT_GRACEFUL_STOP, clear_stop_files, watch_stop_request
from qqbot.greeting import startup_greeting
from qqbot.logstyle import build_handlers
from qqbot.onebot import OneBot
from qqbot.persona_layers import status_line as persona_status_line
from qqbot.promptbudget import estimate_tokens

LOCK_PATH = DATA_DIR / "bot.lock"
EXIT_ALREADY_RUNNING = 3


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着。

    Windows 上 `os.kill(pid, 0)` 不可靠（会真的去 TerminateProcess），走 kernel32；
    POSIX（服务器/Linux）上 `os.kill(pid, 0)` 就是标准做法，不用碰 ctypes。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 进程在，只是不归我们
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def acquire_lock() -> bool:
    """单实例锁：防止重复启动导致一条消息回两遍。"""
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                other = int(LOCK_PATH.read_text(encoding="ascii").strip() or 0)
            except (OSError, ValueError):
                other = 0
            if other and other != os.getpid() and _pid_alive(other):
                return False
            # 残留的僵尸锁：原进程已不在，清掉重来
            try:
                LOCK_PATH.unlink()
            except OSError:
                return False
    return False


def release_lock() -> None:
    try:
        if LOCK_PATH.exists():
            owner = LOCK_PATH.read_text(encoding="ascii").strip()
            if owner == str(os.getpid()):
                LOCK_PATH.unlink()
    except OSError:
        pass


def setup_logging() -> None:
    # 只在输出被重定向到文件/管道时才强制 UTF-8（日志文件方便用编辑器看）。
    # 直接输出到 Windows 控制台时不要动编码：控制台自己会按 Unicode 渲染，
    # 强行改 UTF-8 反而会被按 GBK 解码成乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass

    # 控制台那份给「收到 / 回复」上色（见 qqbot/logstyle.py），文件那份保持纯文本：
    # bot.log 是拿来 grep 的，不能混进转义码。重定向到文件时控制台那份也自动不上色。
    # 无论控制台在不在，都往 data/logs/bot.log 写一份，方便事后排查"为什么进程没了"
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = build_handlers(sys.stderr, log_dir / "bot.log")

    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main() -> int:
    load_env()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config.from_env()

    problems = cfg.validate()
    if problems:
        print("配置有问题，先改 .env：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    # 必须在任何网络动作之前抢锁：重复启动会让同一条消息回两遍
    if not acquire_lock():
        print("已经有一个机器人在运行了，这个窗口会自动关闭。", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    atexit.register(release_lock)
    # 只有抢到锁才清：另一个机器人还在跑时，不能动它的下线请求
    clear_stop_files()

    setup_logging()
    log = logging.getLogger("qqbot")
    log.info("模型 %s @ %s（思考模式: %s）", cfg.model, cfg.base_url, cfg.thinking)
    log.info("机器人 QQ: %s | 接入口: %s", cfg.bot_qq, cfg.ws_url)
    if cfg.dry_run:
        log.warning("DRY-RUN 模式：不会真的调用 DeepSeek，只回显")
    if cfg.group_whitelist:
        log.info("只响应这些群: %s", ", ".join(cfg.group_whitelist))

    onebot = OneBot(cfg.ws_url, cfg.token)
    bot = Bot(cfg, onebot)
    onebot.on_event = bot.on_event

    # 固定前缀是每轮都重发的钱：人设一长，账单悄悄涨。启动时留一行底账，
    # 觉得不对劲就跑 python tools/prompt_budget.py --sections 看谁最贵。
    try:
        group_system = bot._system_prompt(is_group=True)
        log.info(
            "固定前缀：system ≈ %.1fk token（人设 %d 字，占 system 的 %.0f%%）｜"
            "工具另计，明细：python tools/prompt_budget.py",
            estimate_tokens(group_system) / 1000,
            len(cfg.persona),
            len(cfg.persona) / max(1, len(group_system)) * 100,
        )
        log.info("%s", persona_status_line(bot.persona_plan))
    except Exception:  # noqa: BLE001 - 量个账而已，不能拦住启动
        log.debug("前缀预算没算出来", exc_info=True)

    async def _serve() -> int:
        # 后台把 embedding 模型读进内存。91MB 从磁盘加载要好几秒，
        # 放到启动时做，免得第一条消息卡在那里。
        # warm_ready 让上线问候等到 embedding 可用（没开语义就立即就绪）。
        warm_ready = asyncio.Event()
        if cfg.enable_semantic:
            async def _warm() -> None:
                try:
                    await asyncio.to_thread(bot.embedder.encode, ["预热"])
                    log.info("embedding 模型已预热")
                except Exception as exc:  # noqa: BLE001
                    log.warning("预热失败（不影响使用）: %s", exc)
                finally:
                    # 上线问候不用等表情包向量
                    warm_ready.set()
                try:
                    # 表情包逐图索引的向量：和记忆共用 embedding，顺手一起算好
                    n = await asyncio.to_thread(bot.stickers.warm)
                    if n:
                        log.info("表情包逐图向量已就绪：%d 张", n)
                except Exception as exc:  # noqa: BLE001
                    log.warning("表情包向量预热失败（不影响使用）: %s", exc)

            asyncio.create_task(_warm())
        else:
            warm_ready.set()

        # 表情包认图用的 dHash：不依赖 embedding，开了表情包就预热。
        # 收到图时要判断「是不是本鱼自己收来的那张」，临场算 143 张会卡一下。
        async def _warm_sticker_dhash() -> None:
            try:
                n = await asyncio.to_thread(bot.stickers.warm_dhash)
                if n:
                    log.info("表情包 dHash 已就绪：%d 张（认自己收来的图用）", n)
            except Exception as exc:  # noqa: BLE001
                log.warning("表情包 dHash 预热失败（不影响使用）: %s", exc)

        asyncio.create_task(_warm_sticker_dhash())

        # 后台任务：主动私信（没开就什么都不做）
        bot.start_background()

        # 下线公告和连接谁先来都行：正常情况下连接一直跑着，
        # 直到 停止机器人.bat 写 data/stop.request，watcher 播报完再置 stop_event。
        stop_event = asyncio.Event()
        watcher = asyncio.create_task(
            watch_stop_request(onebot, stop_event, prefs=bot.prefs)
        )
        connection = asyncio.create_task(onebot.run())
        stop_wait = asyncio.create_task(stop_event.wait())

        dm_state = ROOT / cfg.presence_dm_state_file

        async def _notify_presence_dm() -> None:
            """好友上线通知：连接稳定一会儿再发，崩溃循环不吵人。

            只发上线；下线不发（正常停止时进程马上要退，异常掉线时进程
            已经没了）。失败只记日志，绝不影响聊天。
            """
            if not cfg.presence_dm_enabled:
                return
            try:
                await onebot.connected.wait()
                if cfg.presence_dm_stable_seconds > 0:
                    await asyncio.sleep(cfg.presence_dm_stable_seconds)
                if stop_event.is_set() or not onebot.connected.is_set():
                    return
                friend_ids = await bot._friend_ids(refresh=True)
                report = await presence.notify_online(
                    onebot, bot.prefs, cfg, friend_ids, state_path=dm_state
                )
                log.info("好友上线通知：%s", report)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 通知失败不影响聊天
                log.exception("好友上线通知失败（不影响使用）")

        presence_dm = asyncio.create_task(_notify_presence_dm())
        presence_beat = asyncio.create_task(
            presence.heartbeat_loop(stop_event, state_path=dm_state)
        )

        async def _greet_on_start() -> None:
            """连上 NapCat、整个启动流程就绪后，让模型写一句上线问候，发到所有群。

            问候是锦上添花：失败只记日志，绝不耽误聊天/下线。
            """
            if not cfg.enable_startup_greeting:
                return
            try:
                await onebot.connected.wait()
                await warm_ready.wait()
                await startup_greeting(bot, onebot, stop_event=stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("上线问候失败（不影响使用）")

        greeting = asyncio.create_task(_greet_on_start())

        async def _catchup_on_connect() -> None:
            """每次上线/重连稳定后，把没接上的私聊补回来。

            补收失败只记日志：它不能拖住聊天，也不能拖住下线。
            """
            if not cfg.enable_catchup_dm:
                return
            try:
                await catchup_watch(bot, onebot, stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("上线补收失败（不影响使用）")

        catchup = asyncio.create_task(_catchup_on_connect())
        try:
            done, _ = await asyncio.wait(
                {connection, stop_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                log.info(
                    "下线请求处理完毕，退出（退出码 %d：bot_loop 不会再拉起）",
                    EXIT_GRACEFUL_STOP,
                )
                return EXIT_GRACEFUL_STOP
            # onebot.run() 正常不会返回；真返回了就把异常抛出来
            exc = connection.exception()
            if exc:
                raise exc
            return 0
        finally:
            tasks = [
                connection,
                watcher,
                stop_wait,
                greeting,
                catchup,
                presence_dm,
                presence_beat,
            ]
            # 直播陪看的后台循环（主人开过才有）
            live_task = getattr(bot, "_bili_live_task", None)
            if live_task is not None:
                tasks.append(live_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 正常退出时记一笔"我还活着"，下次算停机时长以这里为准
            presence.touch(dm_state)
            # 后台的 B 站档案任务和连接池：退出前收掉，别留下半截请求
            try:
                await bot.bili.aclose()
            except Exception:  # noqa: BLE001 - 收尾失败不影响退出
                log.debug("关闭 B 站客户端失败", exc_info=True)
            if getattr(bot, "bili_life", None) is not None:
                try:
                    await bot.bili_life.close()
                except Exception:  # noqa: BLE001
                    log.debug("关闭 B 站生活失败", exc_info=True)
            try:
                bot.live_memes.close()
            except Exception:  # noqa: BLE001
                log.debug("关闭直播弹幕库失败", exc_info=True)
            try:
                bot.live_history.close()
            except Exception:  # noqa: BLE001
                log.debug("关闭直播开播记录库失败", exc_info=True)

    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        log.info("已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
