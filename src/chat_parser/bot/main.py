"""Запуск бота: polling + суточный планировщик в одном процессе.

Бот — это панель управления (токен от @BotFather). Историю чатов он читать
не может и не должен: этим занимается аккаунт-сборщик через MTProto. Две
разные сущности, два разных набора ключей.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, User

from .. import clock, db
from ..config import settings
from ..net import aiogram_proxy, network_hint
from . import format as fmt
from . import jobs
from .auth import AdminOnly
from . import delivery, live
from ..report.daily import DailyReport
from .handlers import KEYBOARD_KEY, KEYBOARD_VERSION, MAIN_KB, router

log = logging.getLogger("chat_parser.bot")

COMMANDS = [
    BotCommand(command="status", description="что загружено и что выполняется"),
    BotCommand(command="extract", description="разобрать обсуждения"),
    BotCommand(command="cluster", description="пересчитать боли"),
    BotCommand(command="top", description="топ болей"),
    BotCommand(command="pain", description="карточка боли по номеру"),
    BotCommand(command="signals", description="последние сигналы"),
    BotCommand(command="report", description="отчёт файлом"),
    BotCommand(command="redo", description="разобрать всё заново"),
    BotCommand(command="retry", description="повторить неудачные обсуждения"),
    BotCommand(command="run", description="полный цикл одной кнопкой"),
    BotCommand(command="chats", description="список чатов"),
    BotCommand(command="people", description="участники: кто из какой компании"),
    BotCommand(command="help", description="справка"),
]


async def _broadcast(bot: Bot, text: str) -> None:
    for admin in settings.admin_ids:
        try:
            for part in fmt.chunks(text):
                await bot.send_message(admin, part)
        except Exception as e:  # noqa: BLE001 — один недоступный админ не валит рассылку
            log.warning("не доставлено %s: %s", admin, e)


async def send_report(bot: Bot, mark_sent: bool = True) -> DailyReport:
    """Итоги дня всем админам: PDF в Telegram и, если настроено, на почту."""
    report = await jobs.run_job(live.build_report(mark_sent))
    await delivery.deliver(bot, settings.admin_ids, report, email=mark_sent)
    return report


async def report_loop(bot: Bot) -> None:
    """Итоги дня в выбранное время. Если в это время бот был выключен —
    досылаются сразу после запуска. Смена времени в боте будит цикл."""
    live.schedule_changed = asyncio.Event()  # событие привязывается к своему циклу asyncio
    while True:
        live.schedule_changed.clear()
        try:
            if await live.report_due():
                await send_report(bot)
            wait = (await live.next_report_at() - clock.now()).total_seconds()
        except jobs.Busy:
            wait = 60  # идёт другая операция — отчёт чуть позже
        except jobs.Cancelled:
            try:
                await live.skip_pending()  # остановили кнопкой ⏹ — сегодня не повторяем
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("не удалось сдвинуть расписание: %s", e)
                wait = 60
        except Exception as e:  # noqa: BLE001
            log.warning("дневной отчёт не собрался: %s: %s", type(e).__name__, e)
            wait = 300
        try:
            await asyncio.wait_for(live.schedule_changed.wait(), timeout=max(wait, 1) + 1)
        except asyncio.TimeoutError:
            pass


async def announce_keyboard(bot: Bot) -> None:
    """Прислать новую клавиатуру один раз после обновления: у пользователя в
    Telegram остаётся старая, пока бот не пришлёт другую."""
    if await db.get_setting(KEYBOARD_KEY) == KEYBOARD_VERSION:
        return
    at = clock.hhmm(*await live.report_time())
    for admin in settings.admin_ids:
        try:
            await bot.send_message(
                admin,
                f"⌨️ Кнопки внизу на месте. Итоги дня приходят в {at} ({clock.tz_label()}) — "
                "поменять время можно кнопкой ⏰ Время отчёта. Кнопка 👤 Инфо о человеке — "
                "записать, из какой компании человек и кем работает. Если кнопки когда-нибудь "
                "пропадут — отправь /start. Подробности — /help",
                reply_markup=MAIN_KB,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("не доставлено %s: %s", admin, e)
    await db.set_setting(KEYBOARD_KEY, KEYBOARD_VERSION)


async def connect_saved(bot: Bot) -> None:
    """Ключи Telegram появились — подключить чаты, сохранённые кнопкой
    «Добавить чат», и предложить загрузить их историю. Токенов модели не
    тратит: это только подключение, разбор — по кнопке."""
    try:
        res = await jobs.run_job(jobs.connect_saved_chats())
    except (jobs.Busy, jobs.Cancelled):
        return  # подключатся по кнопке в /chats или при следующем запуске
    except Exception as e:  # noqa: BLE001
        await _broadcast(bot, f"❌ Не удалось подключить сохранённые чаты: {fmt.esc(e)}")
        return
    text, markup = fmt.fmt_connect_result(res)
    if not text:
        return
    for admin in settings.admin_ids:
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.warning("не доставлено %s: %s", admin, e)


def build_bot() -> Bot:
    """Бот с прокси из TG_PROXY, если он задан."""
    proxy = aiogram_proxy(settings.tg_proxy)
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    return Bot(
        token=settings.tg_bot_token,
        session=session,
        # Превью ссылок выключено: под цитатами ссылки на сообщения, и Telegram
        # иначе прилеплял бы к карточке боли большую плашку первой из них.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def check_bot(bot: Bot) -> User:
    """get_me с понятными ошибками вместо трейсбека aiogram."""
    try:
        return await bot.get_me()
    except TelegramUnauthorizedError:
        raise SystemExit(
            "TG_BOT_TOKEN не подходит — Telegram его не принял. "
            "Возьми токен заново у @BotFather (/mybots -> API Token)."
        ) from None
    except TelegramNetworkError as e:
        raise SystemExit(network_hint(e.message, settings.tg_proxy)) from None


async def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        bot = build_bot()
    except ValueError as e:  # неправильный TG_PROXY
        raise SystemExit(str(e)) from None
    background: list[asyncio.Task] = []
    try:
        # Связь проверяем до сборки диспетчера: router — модульный синглтон,
        # и его можно подключить только к одному диспетчеру за процесс.
        me = await check_bot(bot)
        # Схема идемпотентна: так новые таблицы появляются после обновления
        # кода без ручного init-db.
        await db.apply_schema()
        dp = Dispatcher()
        guard = AdminOnly(settings.admin_ids)
        dp.message.outer_middleware(guard)
        dp.callback_query.outer_middleware(guard)
        dp.include_router(router)
        await bot.set_my_commands(COMMANDS)
        via = f", через прокси {settings.tg_proxy}" if settings.tg_proxy else ""
        log.info("бот @%s запущен%s, админов: %d", me.username, via, len(settings.admin_ids))
        background = [asyncio.create_task(announce_keyboard(bot))]
        if settings.telegram_ready:
            if await jobs.pending_requests():
                background.append(asyncio.create_task(connect_saved(bot)))
            background.append(asyncio.create_task(report_loop(bot)))
        await dp.start_polling(bot)
    finally:
        for t in background:
            t.cancel()
        await db.close_pool()
        await bot.session.close()
