"""Telegram Gateway — 1 process duy nhất nói chuyện với arb bot OKX.

Chạy:  python gateway.py
"""
import logging
import sys
from datetime import time as _time, timezone, timedelta

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
)

import config
import commands
import clients


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    # UTF-8 stdout cho tiếng Việt
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


# Danh sách lệnh hiện trong nút Menu của Telegram (setMyCommands)
BOT_COMMANDS = [
    BotCommand('menu',      '🎛 Bảng lệnh'),
    BotCommand('web',       '🔐 Cấp mật khẩu web'),
    BotCommand('help',      '📖 Danh sách lệnh đầy đủ'),
    BotCommand('dsquyen',   '🔑 Danh sách quyền'),
    BotCommand('capquyen',  'Cấp quyền xem'),
    BotCommand('thuquyen',  'Thu quyền xem'),
]


async def _post_init(app: Application):
    """Sau khi PTB init xong: đăng ký menu lệnh + hiển thị banner."""
    log = logging.getLogger('gateway')
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info(f"Đã đăng ký {len(BOT_COMMANDS)} lệnh vào nút Menu Telegram")
    except Exception as e:
        log.warning(f"set_my_commands fail: {e}")
    log.info("━━━ Telegram Gateway online ━━━")
    log.info(f"Whitelist: {len(config.ALLOWED_CHAT_IDS)} chat IDs")
    log.info(f"Endpoints: {config.BOT_ENDPOINTS}")
    log.info(f"Daily report: {config.DAILY_HOUR:02d}:00 (VN time)")
    log.info(f"Alert poll: {config.ALERT_INTERVAL}s · "
             f"DD ≤ {config.DD_THRESHOLD}% · Profit ≥ {config.PROFIT_THRESHOLD}%")


async def _post_shutdown(app: Application):
    await clients.shutdown()


def build_app() -> Application:
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # ─── Commands ───
    h = app.add_handler
    h(CommandHandler('menu',         commands.cmd_menu))
    h(CommandHandler('web',          commands.cmd_web))
    h(CommandHandler('help',         commands.cmd_help))
    h(CommandHandler('dsquyen',      commands.cmd_dsquyen))
    h(CommandHandler('capquyen',     commands.cmd_capquyen))
    h(CommandHandler('thuquyen',     commands.cmd_thuquyen))

    # ─── Scheduler ───
    jq = app.job_queue
    if jq is not None:
        # Alerts: mỗi N giây (drawdown, profit, bot offline)
        jq.run_repeating(
            commands.job_alerts,
            interval=config.ALERT_INTERVAL,
            first=30,
            name='alerts',
        )
        # Streak check: mỗi 5 phút
        jq.run_repeating(
            commands.job_streak_check,
            interval=300,
            first=120,
            name='streak',
        )
        # Daily summary: hàng ngày lúc DAILY_HOUR (giờ VN = UTC+7)
        vn_tz = timezone(timedelta(hours=7))
        jq.run_daily(
            commands.job_daily,
            time=_time(hour=config.DAILY_HOUR, minute=0, tzinfo=vn_tz),
            name='daily',
        )
    else:
        logging.getLogger('gateway').warning(
            "JobQueue không khả dụng — cài thêm: pip install \"python-telegram-bot[job-queue]\""
        )

    return app


def main():
    setup_logging()
    config.validate()
    app = build_app()
    app.run_polling(drop_pending_updates=True, allowed_updates=None)


if __name__ == '__main__':
    main()
