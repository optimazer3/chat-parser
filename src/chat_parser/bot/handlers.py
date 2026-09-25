"""Команды и кнопки бота.

Всё, что делается из консоли, доступно отсюда. Дорогие операции (разбор
сигналов моделью) никогда не запускаются на всю очередь без явного выбора,
а любую долгую операцию можно остановить кнопкой ⏹. Расход токенов
пользователю не показывается — только по скрытой команде /usage.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import tempfile
import time
import uuid
from pathlib import Path

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
)

from .. import clock, db, usage
from ..config import settings
from ..ingest import collector
from .. import people
from ..links import QUOTE_MESSAGE_SQL, BadChatRef, chat_link, message_link, normalize_chat_ref
from ..llm import build_llm
from ..quotes import pick_quotes
from . import format as fmt
from . import jobs

router = Router()

# Bot API отдаёт боту файлы не больше 20 МБ. Экспорт живого чата легко больше.
MAX_UPLOAD = 20 * 1024 * 1024

BTN_STATUS = "📊 Статус"
BTN_EXTRACT = "🧠 Разобрать"
BTN_CLUSTER = "🧩 Пересчитать боли"
BTN_TOP = "🔝 Топ болей"
BTN_REPORT = "📄 Отчёт"
BTN_HELP = "❓ Помощь"
BTN_ADD = "➕ Добавить чат"
BTN_TOP_MONTH = "🔝 Топ болей за месяц"
BTN_CHATS = "💬 Список чатов"
BTN_PERSON = "👤 Инфо о человеке"
BTN_TIME = "⏰ Время отчёта"
BTN_SEND_NOW = "📤 Отправить отчёт сейчас"  # временная, для проверки

# Меняется вместе с набором кнопок: бот один раз пришлёт новую клавиатуру.
KEYBOARD_KEY = "keyboard_version"
KEYBOARD_VERSION = "6"

# Прежние кнопки (статус, разобрать, отчёт…) убраны с клавиатуры, но их
# обработчики оставлены: у кого-то в Telegram ещё может висеть старая.
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TOP_MONTH)],
        [KeyboardButton(text=BTN_CHATS), KeyboardButton(text=BTN_ADD)],
        [KeyboardButton(text=BTN_PERSON), KeyboardButton(text=BTN_TIME)],
        [KeyboardButton(text=BTN_SEND_NOW)],  # временная — убрать после проверки
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def help_text(at: str = "22:00") -> str:
    """at — во сколько приходят итоги дня, «21:30»."""
    budget = (f"не больше {settings.live_daily_budget:g} ₽ в день"
              if settings.live_daily_budget > 0 else "без дневного лимита")
    return f"""👋 <b>Я слежу за чатами рынка оптики</b>

Читаю переписку владельцев оптик, продавцов, врачей и покупателей и нахожу,
на что люди жалуются, что ищут и каких решений им не хватает.

<b>Как это работает</b>
• Каждый день в <b>{at}</b> ({clock.tz_label()}) я забираю переписку подключённых
  чатов за сутки (для контекста — и за сутки до них), разбираю её и присылаю
  <b>итоги дня файлом PDF</b>: главное за день, что было в каждом чате, новые
  боли, самые острые жалобы и всплески — с цитатами и ссылками на сообщения.
  Копия приходит на почту, если она настроена. В остальное время я не пишу.
• Под каждой цитатой видно, кто её сказал. Кто из какой компании и кем
  работает — можно записать, я и сам подскажу, если человек рассказал о себе.
• Каждая жалоба подтверждена дословной цитатой — выдуманное я отбрасываю.
• На автоматический разбор трачу {budget}.

<b>Кнопки</b>
🔝 <b>Топ болей за месяц</b> — главные боли за последние 30 дней. Рядом с каждой
   ссылка /pain_… — нажми, пришлю подробности, цитаты и идеи решений.
💬 <b>Список чатов</b> — за чем я слежу; там же участники каждого чата и удаление.
➕ <b>Добавить чат</b> — пришли ссылку на чат, и я начну за ним следить.
👤 <b>Инфо о человеке</b> — пришли @username и через запятую компанию, роль,
   заметку: <i>@ivan_optika Оптика Люкс, владелец</i>.
⏰ <b>Время отчёта</b> — во сколько присылать итоги дня.
📤 <b>Отправить отчёт сейчас</b> — временно, для проверки: итоги за последние
   сутки сразу (PDF и копия на почту). Отчёт по расписанию придёт как обычно.

<b>Полезно знать</b>
• Топ пополняется вместе с итогами дня.
• Историю чата можно прислать и файлом: Telegram Desktop → чат → ⋮ →
  «Экспорт истории чата» → формат JSON → пришли мне <code>result.json</code>.
• Сам я разбираю только переписку последних суток. Архив из файла — вручную: /extract.
• Нажми /who_… под цитатой — откроется карточка человека: компания, роль, заметка.
• Любую долгую операцию можно остановить кнопкой ⏹.
• Остальные команды — в меню слева от поля ввода.
"""


class LiveMessage:
    """Сообщение-прогресс, которое правится не чаще раза в interval секунд.

    Telegram ограничивает частоту правок: обновлять на каждом обсуждении —
    быстрый путь к 429. Кнопка ⏹ держится под сообщением, пока идёт работа
    (правка без reply_markup её бы сняла), и убирается в финале.
    """

    def __init__(
        self, message: Message, markup: InlineKeyboardMarkup | None = None,
        interval: float = 3.0,
    ) -> None:
        self.message = message
        self.markup = markup
        self.interval = interval
        self._last = 0.0
        self._shown = message.text or ""

    async def set(self, text: str, force: bool = False, final: bool = False) -> None:
        force = force or final
        if text == self._shown and not final:
            return
        now = time.monotonic()
        if not force and now - self._last < self.interval:
            return
        try:
            await self.message.edit_text(text, reply_markup=None if final else self.markup)
        except TelegramAPIError:
            # Итог терять нельзя: если правка не прошла, шлём отдельным сообщением.
            if final:
                await self.message.answer(text)
            return
        self._shown = text
        self._last = now


def _stop_kb(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏹ Остановить", callback_data=f"stop:{job_id}")]]
    )


async def _start_live(message: Message, text: str) -> tuple[str, LiveMessage]:
    """Сообщение с прогрессом и кнопкой ⏹ для новой операции."""
    job_id = jobs.new_job_id()
    kb = _stop_kb(job_id)
    return job_id, LiveMessage(await message.answer(text, reply_markup=kb), markup=kb)


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
            if row
        ]
    )


def _busy(e: jobs.Busy) -> str:
    job = jobs.current() or {}
    return fmt.fmt_busy(e.what, job.get("progress", ""))


async def _reply_long(message: Message, text: str, **kwargs) -> None:
    parts = fmt.chunks(text)
    for i, part in enumerate(parts):
        # клавиатуру вешаем на последний кусок
        await message.answer(part, **(kwargs if i == len(parts) - 1 else {}))


async def _drop_markup(call: CallbackQuery) -> None:
    if call.message is not None:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass


# ------------------------------------------------------------- справка/статус


# ------------------------------------------------ ответ на подсказку бота
#
# «Пришли время», «пришли @ник и сведения» — бот ждёт следующее сообщение.
# ForceReply здесь нельзя: в Telegram он занимает место клавиатуры бота, и
# кнопки внизу пропадают до следующего /start.

AWAIT_SECONDS = 15 * 60
_awaiting: dict[int, tuple[str, float]] = {}  # кто -> (чего ждём, до какого момента)
KEYBOARD_TEXTS = {BTN_STATUS, BTN_EXTRACT, BTN_CLUSTER, BTN_TOP, BTN_REPORT, BTN_HELP,
                  BTN_ADD, BTN_TOP_MONTH, BTN_CHATS, BTN_PERSON, BTN_TIME, BTN_SEND_NOW}
CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel")]]
)


def _await_answer(user: User | None, what: str) -> None:
    if user is not None:
        _awaiting[user.id] = (what, time.monotonic() + AWAIT_SECONDS)


class AwaitingAnswer(Filter):
    """Это ответ на подсказку. Нажали кнопку внизу или команду — ждать перестаём."""

    async def __call__(self, message: Message) -> bool | dict:
        item = _awaiting.pop(message.from_user.id, None) if message.from_user else None
        if item is None:
            return False
        what, deadline = item
        text = (message.text or "").strip()
        if (time.monotonic() > deadline or not text or text.startswith("/")
                or text in KEYBOARD_TEXTS):
            return False
        return {"awaiting": what}


@router.message(F.text, AwaitingAnswer())
async def on_awaited_answer(message: Message, awaiting: str) -> None:
    text = (message.text or "").strip()
    if awaiting == "time":
        await _time_answer(message, text)
    elif awaiting == "person":
        await _person_info(message, text)
    elif awaiting.startswith(("edit:", "note:")):
        kind, label = awaiting.split(":", 1)
        await _person_edit(message, "✏️" if kind == "edit" else "📝", label, text)


EDIT_PROMPT_RE = r"^(✏️|📝) .*?(u:[0-9a-f]{8})"


@router.message(F.reply_to_message.text.regexp(EDIT_PROMPT_RE))
async def on_person_edit_reply(message: Message) -> None:
    """Ответили на «✏️ Компания и роль для u:…» или «📝 Заметка для u:…» сами."""
    m = re.match(EDIT_PROMPT_RE, message.reply_to_message.text or "")
    if m:
        await _person_edit(message, m.group(1), m.group(2), (message.text or "").strip())


async def _person_edit(message: Message, kind: str, label: str, text: str) -> None:
    if not text:
        await message.answer("Не понял ответ — напиши текстом.")
        return
    pool = await db.get_pool()
    if kind == "✏️":
        company, role = people.split_company_role(text)
        ok = await people.set_company_role(pool, label, company, role)
    else:
        ok = await people.set_note(pool, label, None if text in ("-", "—") else text)
    if not ok:
        await message.answer("Такого участника нет в базе.")
        return
    await _send_person(message, label, prefix="✅ Сохранил.\n\n")


TIME_PROMPT = "⏰ Во сколько присылать итоги дня?"
PERSON_PROMPT = "👤 Информация о человеке"
PERSON_EXAMPLES = (
    "<i>@ivan_optika Оптика Люкс, владелец, знакомы по выставке</i>\n"
    "<i>@maria_opt Линзы Плюс</i>\n"
    "<i>@olga_lens роль: оптометрист</i>"
)


@router.message(F.reply_to_message.text.startswith(TIME_PROMPT))
async def on_time_reply(message: Message) -> None:
    await _time_answer(message, message.text or "")


async def _time_answer(message: Message, text: str) -> None:
    from . import live

    parsed = live.parse_time(text)
    if parsed is None:
        await message.answer(
            f"{TIME_PROMPT}\n\nНе понял «{fmt.esc(text)}». Напиши время, например "
            "<b>21:30</b> или <b>9:00</b>.",
            reply_markup=CANCEL_KB,
        )
        _await_answer(message.from_user, "time")
        return
    await _apply_time(message, *parsed)


@router.message(F.reply_to_message.text.startswith(PERSON_PROMPT))
async def on_person_info_reply(message: Message) -> None:
    await _person_info(message, message.text or "")


@router.message(CommandStart(deep_link=True))
async def cmd_start_link(message: Message, command: CommandObject) -> None:
    """Ссылки из PDF итогов дня: t.me/<бот>?start=pain_12 или start=who_1a2b3c4d."""
    arg = (command.args or "").strip()
    if m := re.fullmatch(r"pain_(\d+)", arg):
        await _send_pain(message, m.group(1))
    elif m := re.fullmatch(r"who_([0-9a-f]{8})", arg):
        await _send_person(message, "u:" + m.group(1))
    else:
        await cmd_help(message)


@router.message(CommandStart())
@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    from . import live

    at = clock.hhmm(*await live.report_time())
    await message.answer(help_text(at), reply_markup=MAIN_KB)


@router.message(Command("status"))
@router.message(F.text == BTN_STATUS)
async def cmd_status(message: Message) -> None:
    pool = await db.get_pool()
    chats = await pool.fetch(
        """
        select c.title, cu.backfill_done, cu.last_run, cu.retry_after,
               (select count(*) from messages m where m.chat_id = c.id) msgs
          from chats c left join cursors cu on cu.chat_id = c.id
         where c.is_active order by c.id
        """
    )
    pending = await jobs.queue_size()
    counts = await pool.fetchrow(
        "select (select count(*) from signals) s, (select count(*) from clusters) c,"
        " (select count(*) from threads where status = 'failed') f"
    )
    text = fmt.fmt_status(list(chats), pending, await jobs.last_pipeline_at())
    text += f"\nСигналов: <b>{counts['s']}</b> · болей: <b>{counts['c']}</b>"
    if counts["f"]:
        text += f" · не разобралось: {counts['f']} (/retry)"
    job = jobs.current()
    if job:
        progress = f": {fmt.esc(job['progress'])}" if job["progress"] else ""
        text += f"\n\n⏳ Сейчас идёт {fmt.esc(job['name'])}{progress}"
        # Кнопка и здесь: у фоновой операции (подключение сохранённых чатов при
        # запуске) нет своего сообщения с прогрессом.
        await _reply_long(message, text, reply_markup=_stop_kb(job["id"]))
        return
    await _reply_long(message, text)


# ------------------------------------------------------------- разбор сигналов


async def _extract_menu(message: Message, prefix: str = "") -> None:
    pending = await jobs.queue_size()
    text = prefix + fmt.fmt_estimate(pending, await jobs.seconds_per_thread())
    choices = fmt.extract_choices(pending)
    if not choices:
        await message.answer(text)
        return
    await message.answer(
        text + "\n\nСколько разобрать?",
        reply_markup=_kb(
            [(label, f"ex:{value}") for label, value in choices], [("Отмена", "cancel")]
        ),
    )


async def _do_extract(message: Message, limit: int | None) -> None:
    job_id, live = await _start_live(message, "🧠 Запускаю разбор…")
    seen = {"done": 0, "signals": 0}

    async def progress(done: int, total: int, st: dict) -> None:
        seen.update(done=done, signals=st["signals"])
        await live.set(fmt.fmt_extract_progress(done, total, st))

    try:
        stats = await jobs.run_job(jobs.run_extract(progress, limit, job_id))
    except jobs.Cancelled:
        await live.set(fmt.fmt_stopped("разбор", seen), final=True)
        return
    except jobs.Busy as e:
        await live.set(_busy(e), final=True)
        return
    except Exception as e:  # noqa: BLE001 — показать причину, не ронять бота
        await live.set(f"❌ Разбор не удался: {type(e).__name__}: {fmt.esc(e)}", final=True)
        return

    await live.set(fmt.fmt_extract_result(stats), final=True)
    nxt = []
    if stats.get("pending_left"):
        nxt.append(("🧠 Разобрать ещё 20", "ex:20"))
    nxt.append(("🧩 Пересчитать боли", "cl"))
    retry = [("🔁 Повторить неудачные", "retry")] if stats["failed"] else []
    await message.answer("Что дальше?", reply_markup=_kb(nxt, retry, [("Отмена", "cancel")]))


@router.message(Command("extract"))
async def cmd_extract(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    if arg.isdigit() and int(arg) > 0:
        await _do_extract(message, int(arg))
    elif arg in ("all", "все", "всё"):
        await _do_extract(message, None)
    else:
        await _extract_menu(message)


@router.message(F.text == BTN_EXTRACT)
async def btn_extract(message: Message) -> None:
    await _extract_menu(message)


@router.callback_query(F.data.startswith("ex:"))
async def cb_extract(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    value = call.data.split(":", 1)[1]
    if call.message is not None:
        await _do_extract(call.message, None if value == "all" else int(value))


@router.message(Command("redo"))
async def cmd_redo(message: Message) -> None:
    pool = await db.get_pool()
    n = await pool.fetchval(
        "select count(*) from threads where status in ('extracted', 'failed')"
    )
    if not n:
        await message.answer("Разбирать заново нечего: ещё ничего не разобрано.")
        return
    await message.answer(
        f"Разобрать заново <b>{fmt.discussions(n)}</b>?\n"
        "Они вернутся в очередь, а их сигналы перезапишутся при следующем разборе — "
        "дублей не будет. Нужно, если поменялись настройки разбора.",
        reply_markup=_kb([(f"Да, вернуть {n}", "redo:yes"), ("Отмена", "cancel")]),
    )


async def _reset_and_menu(message: Message, kind: str) -> None:
    try:
        n = await jobs.reset_queue(kind)
    except jobs.Busy as e:
        await message.answer(_busy(e))
        return
    if not n:
        await message.answer(
            "Неудачных обсуждений нет — всё разобралось." if kind == "failed"
            else "Возвращать нечего."
        )
        return
    await _extract_menu(message, prefix=f"Вернул в очередь: {fmt.discussions(n)}.\n")


@router.callback_query(F.data == "redo:yes")
async def cb_redo(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is not None:
        await _reset_and_menu(call.message, "redo")


@router.message(Command("retry"))
async def cmd_retry(message: Message) -> None:
    await _reset_and_menu(message, "failed")


@router.callback_query(F.data == "retry")
async def cb_retry(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is not None:
        await _reset_and_menu(call.message, "failed")


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: CallbackQuery) -> None:
    what = jobs.cancel(call.data.split(":", 1)[1])
    if what is None:
        await call.answer("Эта операция уже завершилась")
        await _drop_markup(call)
        return
    # Итоговое сообщение напишет обработчик, который ждал операцию.
    await call.answer("Останавливаю…")


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    _awaiting.pop(call.from_user.id, None)
    await call.answer("Отменено")
    await _drop_markup(call)


# ------------------------------------------------------------------- боли


async def _send_top(message: Message, limit: int = 10) -> None:
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select id, audience, label, score, n_authors, n_chats, n_signals
          from clusters order by audience, score desc limit $1
        """,
        limit,
    )
    await _reply_long(message, fmt.fmt_top(list(rows)))


async def _do_cluster(message: Message) -> None:
    job_id, live = await _start_live(message, "🧩 Запускаю пересчёт болей…")
    try:
        stats = await jobs.run_job(jobs.run_cluster(lambda text: live.set(text), job_id))
    except jobs.Cancelled:
        await live.set(fmt.fmt_stopped("пересчёт болей"), final=True)
        return
    except jobs.Busy as e:
        await live.set(_busy(e), final=True)
        return
    except Exception as e:  # noqa: BLE001
        await live.set(f"❌ Пересчёт не удался: {type(e).__name__}: {fmt.esc(e)}", final=True)
        return
    if not stats["clusters_total"]:
        await live.set(
            "Собирать пока нечего: нужно хотя бы 4 сигнала от одной группы людей, "
            "и чтобы они повторялись. Разбери больше обсуждений — 🧠 Разобрать.",
            final=True,
        )
        return
    await live.set(
        f"✅ Болей найдено: <b>{stats['clusters_total']}</b>, описаний: {stats['cards']}. "
        "Отчёт обновлён.",
        final=True,
    )
    await _send_top(message)


@router.message(Command("cluster"))
@router.message(F.text == BTN_CLUSTER)
async def cmd_cluster(message: Message) -> None:
    await _do_cluster(message)


@router.callback_query(F.data == "cl")
async def cb_cluster(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is not None:
        await _do_cluster(call.message)


AUDIENCE_ORDER = ["owner", "staff", "optometrist", "supplier", "customer", "unknown"]
MONTH_PER_AUDIENCE = 7


@router.message(F.text == BTN_TOP_MONTH)
async def btn_top_month(message: Message) -> None:
    """Боли по сигналам за последние 30 дней: кто и сколько говорил за месяц."""
    pool = await db.get_pool()
    rows = [dict(r) for r in await pool.fetch(
        """
        select c.id, c.audience, c.label,
               count(*) n_signals,
               count(distinct s.author_label) n_authors,
               count(distinct s.chat_id) n_chats
          from clusters c join signals s on s.cluster_id = c.id
         where s.ts >= now() - interval '30 days'
         group by c.id, c.audience, c.label
         order by n_authors desc, n_signals desc, c.id
        """
    )]
    if not rows:
        await message.answer(
            "За последние 30 дней болей пока нет. Топ пополняется вечером, вместе с "
            "итогами дня. Боли за всё время — /top"
        )
        return
    picked = []
    for aud in AUDIENCE_ORDER + sorted({r["audience"] for r in rows} - set(AUDIENCE_ORDER)):
        picked += [r for r in rows if r["audience"] == aud][:MONTH_PER_AUDIENCE]
    await _reply_long(message, fmt.fmt_top(picked, title="🔝 <b>Топ болей за месяц</b>"))


@router.message(Command("top"))
@router.message(F.text == BTN_TOP)
async def cmd_top(message: Message, command: CommandObject | None = None) -> None:
    limit = 10
    args = (command.args or "").strip() if command else ""
    if args.isdigit():
        limit = min(int(args), 30)
    await _send_top(message, limit)


@router.message(Command(re.compile(r"pain_(\d+)")))
@router.message(Command("pain"))
async def cmd_pain(message: Message, command: CommandObject) -> None:
    # /pain_12 — нажимаемая ссылка из топа; /pain 12 — если набрали руками
    if command.regexp_match is not None:
        arg = command.regexp_match.group(1)
    else:
        arg = (command.args or "").strip().lstrip("#")
    await _send_pain(message, arg)


async def _send_pain(message: Message, arg: str) -> None:
    if not arg.isdigit():
        await message.answer("Открой 🔝 Топ болей и нажми на /pain_… рядом с нужной болью.")
        return
    pool = await db.get_pool()
    cluster = await pool.fetchrow("select * from clusters where id = $1", int(arg))
    if cluster is None:
        await message.answer(
            "Такой боли уже нет — номера меняются после пересчёта. Открой свежий 🔝 Топ болей."
        )
        return
    signals = await _signals_with_links(
        "where s.cluster_id = $1 order by s.intensity desc, s.id", cluster["id"]
    )
    card = cluster["card"]
    card = json.loads(card) if isinstance(card, str) else card
    await _reply_long(message, fmt.fmt_card(cluster, pick_quotes(card, signals)))


@router.message(Command("signals"))
async def cmd_signals(message: Message, command: CommandObject) -> None:
    limit = 10
    if command.args and command.args.strip().isdigit():
        limit = min(int(command.args.strip()), 30)
    rows = await _signals_with_links("order by s.id desc limit $1", limit)
    await _reply_long(message, fmt.fmt_signals(rows))


async def _signals_with_links(tail: str, *args) -> list[dict]:
    """Сигналы вместе со ссылкой на сообщение, где стоит цитата."""
    pool = await db.get_pool()
    rows = await pool.fetch(
        f"""
        select s.id, s.type, s.audience, s.summary, s.evidence_quote, s.intensity,
               s.chat_id, {QUOTE_MESSAGE_SQL} as mid, c.username, s.author_label, a.name a_name, a.company a_company, a.role a_role
          from signals s join chats c on c.id = s.chat_id
          left join authors a on a.author_label = s.author_label
        {tail}
        """,
        *args,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["link"] = message_link(d["chat_id"], d["username"], d["mid"])
        out.append(d)
    return out


async def _send_report(message: Message) -> None:
    if not jobs.REPORT_PATH.is_file():
        await message.answer("Отчёта ещё нет. Сначала 🧩 Пересчитать боли.")
        return
    await message.answer_document(FSInputFile(jobs.REPORT_PATH), caption="Полный отчёт")


@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def cmd_report(message: Message) -> None:
    await _send_report(message)


# ------------------------------------------------------------- полный цикл


async def _do_run(message: Message, extract_limit: int | None) -> None:
    job_id, live = await _start_live(message, "Запускаю полный цикл…")
    since = await jobs.last_pipeline_at()
    try:
        stats = await jobs.run_job(
            jobs.run_pipeline(lambda text: live.set(text), extract_limit, job_id)
        )
    except jobs.Cancelled:
        await live.set(fmt.fmt_stopped("полный цикл"), final=True)
        return
    except jobs.Busy as e:
        await live.set(_busy(e), final=True)
        return
    except Exception as e:  # noqa: BLE001
        await live.set(f"❌ Не удалось: {type(e).__name__}: {fmt.esc(e)}", final=True)
        return

    new_msgs, new_signals = await jobs.since_counts(since)
    pool = await db.get_pool()
    top = await pool.fetch(
        "select id, label, score, n_authors from clusters order by score desc limit 5"
    )
    await live.set("✅ Готово", final=True)
    await _reply_long(message, fmt.fmt_digest(stats, new_signals, new_msgs, list(top)))
    await _send_report(message)


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    pending = await jobs.queue_size()
    if pending > settings.bot_confirm_threshold:
        estimate = fmt.fmt_estimate(pending, await jobs.seconds_per_thread())
        await message.answer(
            f"{estimate}\n\nПолный цикл разберёт их все. Точно?",
            reply_markup=_kb(
                [(f"Все {fmt.fmt_num(pending)}", "run:all"), ("Только 20", "run:20")],
                [("Отмена", "cancel")],
            ),
        )
        return
    await _do_run(message, None)


@router.callback_query(F.data.startswith("run:"))
async def cb_run(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    value = call.data.split(":", 1)[1]
    if call.message is not None:
        await _do_run(call.message, None if value == "all" else int(value))


# ------------------------------------------------------------------ чаты


@router.message(F.text == BTN_CHATS)
@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select c.id, c.title, c.username, c.link, c.is_active, cu.last_run, cu.note,
               (select count(*) from messages m where m.chat_id = c.id) msgs,
               (select max(message_id) from messages m where m.chat_id = c.id) last_msg,
               (select count(*) from threads t where t.chat_id = c.id) threads,
               (select count(*) from threads t
                 where t.chat_id = c.id and t.status = 'extracted') done,
               (select count(*) from signals s where s.chat_id = c.id) signals
          from chats c left join cursors cu on cu.chat_id = c.id
         order by c.id
        """
    )
    waiting = await jobs.pending_requests()
    if not rows and not waiting:
        await message.answer(
            "Чатов пока нет. Пришли файл <code>result.json</code> с историей чата "
            "или нажми ➕ Добавить чат."
        )
        return
    lines = ["💬 <b>Чаты</b>", ""]
    for r in rows:
        link = chat_link(r["id"], r["username"], r["last_msg"], r["link"])
        title = fmt.esc(r["title"] or "без названия")
        name = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else (
            f"<b>{title}</b>"
        )
        updated = (f"обновлён {fmt.when(r['last_run'], with_year=True)}" if r["last_run"]
                   else "история ещё не загружена")
        lines.append(f"• {name}")
        lines.append(f"   {fmt.messages_word(r['msgs'])} · {updated}")
        lines.append(
            f"   обсуждений: {fmt.fmt_num(r['threads'])}, разобрано: {fmt.fmt_num(r['done'])}, "
            f"найдено сигналов: {fmt.fmt_num(r['signals'])}"
        )
        if not r["is_active"]:
            lines.append(f"   ⛔ отключён: {fmt.esc(r['note'] or 'нет доступа')}")
        lines.append("")
    rows_kb: list[list[tuple[str, str]]] = [
        [(f"👥 {_short(r['title'] or 'без названия', 22)}", f"ppl:{r['id']}"),
         ("🗑 Удалить", f"del:{r['id']}")]
        for r in rows
    ]
    if waiting:
        why = ("нужны ключи Telegram API в настройках" if not settings.telegram_ready
               else "подключу по кнопке ниже")
        lines.append(f"⏳ <b>Ждут подключения</b> ({why}):")
        for w in waiting:
            note = {"join_pending": " — ждёт одобрения админа чата",
                    "failed": f" — не получилось: {fmt.esc(w['note'] or '')}"}.get(w["status"], "")
            lines.append(f"• {fmt.esc(w['link'])}{note}")
        rows_kb += [[(f"🗑 Убрать: {_short(w['link'].removeprefix('https://'))}",
                      f"delreq:{w['id']}")] for w in waiting]
        if settings.telegram_ready:
            rows_kb.append([("🔌 Подключить сейчас", "connect:saved")])
    text = "\n".join(lines).rstrip()
    if rows_kb:
        await _reply_long(message, text, reply_markup=_kb(*rows_kb))
    else:
        await _reply_long(message, text)


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@router.callback_query(F.data.startswith("del:"))
async def cb_delete_ask(call: CallbackQuery) -> None:
    await call.answer()
    if call.message is None:
        return
    chat_id = int(call.data.split(":", 1)[1])
    pool = await db.get_pool()
    info = await pool.fetchrow(
        """
        select title,
               (select count(*) from messages where chat_id = $1) messages,
               (select count(*) from signals where chat_id = $1) signals
          from chats where id = $1
        """,
        chat_id,
    )
    if info is None:
        await call.message.answer("Этого чата уже нет в списке.")
        return
    await call.message.answer(
        f"Удалить чат <b>{fmt.esc(info['title'])}</b>?\n\n"
        f"Я перестану за ним следить и удалю из базы его "
        f"{fmt.messages_word(info['messages'])}, обсуждения и "
        f"{fmt.signals_word(info['signals'])}. Боли пересчитаются без него.\n"
        "Из самого чата в Telegram аккаунт не выйдет.",
        reply_markup=_kb([("🗑 Да, удалить", f"delok:{chat_id}"), ("Отмена", "cancel")]),
    )


@router.callback_query(F.data.startswith("delok:"))
async def cb_delete(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    try:
        res = await jobs.delete_chat(int(call.data.split(":", 1)[1]))
    except jobs.Busy as e:
        await call.message.answer(_busy(e))
        return
    if res is None:
        await call.message.answer("Этого чата уже нет в списке.")
        return
    text = (f"✅ Чат <b>{fmt.esc(res['title'])}</b> удалён: "
            f"{fmt.messages_word(res['messages'])}, {fmt.signals_word(res['signals'])}.")
    if res["pains_removed"]:
        text += f"\nБолей, в которых не осталось сигналов: {res['pains_removed']} — убрал."
    await call.message.answer(text)


@router.callback_query(F.data.startswith("delreq:"))
async def cb_delete_request(call: CallbackQuery) -> None:
    link = await jobs.delete_request(int(call.data.split(":", 1)[1]))
    await call.answer("Убрал из ожидания" if link else "Уже убрано")
    if call.message is not None and link:
        await call.message.answer(f"Убрал из ожидания: {fmt.esc(link)}")


# ----------------------------------------------------------- добавление чата

ADD_HINT = (
    "Пришли ссылку на чат одним сообщением:\n"
    "• <code>t.me/имя_чата</code> или <code>@имя_чата</code> — открытый чат\n"
    "• <code>t.me/+…</code> — приглашение в закрытый чат"
)
LINK_FILTER = F.text.regexp(
    r"^\s*(?:(?:https?://)?(?:www\.)?(?:t|telegram)\.me/\S+|@[A-Za-z][A-Za-z0-9_]{3,31})\s*$"
)

# Токен -> ссылка: callback_data ограничен 64 байтами, ссылку туда не засунуть.
_pending_joins: dict[str, str] = {}


@router.message(F.text == BTN_ADD)
async def btn_add(message: Message) -> None:
    await message.answer("➕ " + ADD_HINT)


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, command: CommandObject) -> None:
    if not (command.args or "").strip():
        await message.answer(ADD_HINT)
        return
    await _add_chat(message, command.args)


@router.message(LINK_FILTER)
async def on_chat_link(message: Message) -> None:
    await _add_chat(message, message.text or "")


async def _add_chat(message: Message, raw: str) -> None:
    try:
        link = normalize_chat_ref(raw)
    except BadChatRef as e:
        await message.answer(f"🤔 {fmt.esc(e)}")
        return

    if not settings.telegram_ready:
        status = await jobs.save_chat_request(link)
        if status == "connected":
            await message.answer("Этот чат уже подключён — он есть в /chats.")
        elif status == "exists":
            await message.answer("Этот чат уже в списке ожидания — подключу его, как только смогу.")
        else:
            await message.answer(
                f"📝 Запомнил: {fmt.esc(link)}\n\n"
                "Читать чаты сам я смогу, когда в настройках появятся ключи Telegram API "
                "(TG_API_ID и TG_API_HASH). Как только они будут — подключу этот чат и "
                "загружу его историю без твоего участия.\n\n"
                "А пока историю можно прислать файлом: Telegram Desktop → чат → ⋮ → "
                "Экспорт истории чата → формат JSON."
            )
        return
    await _connect_and_load(message, link, join=False)


async def _connect_and_load(message: Message, link: str, join: bool) -> None:
    status = await message.answer("🔌 Подключаюсь к чату…")
    try:
        chat_id = await jobs.connect_chat(link, join)
    except collector.NeedsJoin as e:
        token = uuid.uuid4().hex[:8]
        _pending_joins[token] = link
        await status.edit_text(
            f"{fmt.esc(e)}\n\n⚠️ Вступление в чаты — самое рискованное для аккаунта "
            "действие: Telegram может ограничить аккаунт, если вступать часто. "
            "Держись в пределах 5–10 чатов в сутки.",
            reply_markup=_kb([("Вступить и добавить", f"join:{token}")],
                             [("Отмена", f"drop:{token}")]),
        )
        return
    except collector.JoinPending:
        await status.edit_text(
            "⏳ Это закрытый чат с одобрением заявок: заявку на вступление я отправил. "
            "Как только админ чата её одобрит, подключу чат сам — повторять не нужно."
        )
        return
    except jobs.Busy as e:
        await status.edit_text(_busy(e))
        return
    except Exception as e:  # noqa: BLE001 — показать причину, не ронять бота
        await status.edit_text(f"❌ Не получилось подключить чат: {fmt.esc(e)}")
        return

    pool = await db.get_pool()
    title = await pool.fetchval("select title from chats where id = $1", chat_id)
    await status.edit_text(f"✅ Подключил чат <b>{fmt.esc(title)}</b>.")
    await _load_history(message, [chat_id])


async def _load_history(message: Message, chat_ids: list[int]) -> None:
    job_id, live = await _start_live(message, "📥 Загружаю историю…")
    seen = {"n": 0}

    async def progress(n: int) -> None:
        seen["n"] = n
        await live.set(
            f"📥 Загружаю историю: <b>{fmt.messages_word(n)}</b>\n\n"
            "<i>Можно остановить — загруженное сохранится, а в следующий раз "
            "загрузка продолжится с того же места.</i>"
        )

    try:
        res = await jobs.run_job(jobs.run_history(chat_ids, progress, job_id))
    except jobs.Cancelled:
        await live.set(
            f"⏹ <b>Загрузка остановлена</b>. Сохранено: {fmt.messages_word(seen['n'])}.\n"
            "Продолжу с того же места при следующем обновлении.",
            final=True,
        )
        return
    except jobs.Busy as e:
        await live.set(_busy(e), final=True)
        return
    except Exception as e:  # noqa: BLE001
        await live.set(f"❌ Загрузка не удалась: {fmt.esc(e)}", final=True)
        return
    await live.set(
        f"✅ Загружено: <b>{fmt.messages_word(res['saved'])}</b>\n"
        f"Обсуждений в чате: {fmt.fmt_num(res['threads'])}",
        final=True,
    )
    await _extract_menu(message)


@router.callback_query(F.data.startswith("join:"))
async def cb_join(call: CallbackQuery) -> None:
    link = _pending_joins.pop(call.data.split(":", 1)[1], None)
    if not link or call.message is None:
        await call.answer("Запрос устарел — пришли ссылку ещё раз", show_alert=True)
        return
    await call.answer()
    await _drop_markup(call)
    await _connect_and_load(call.message, link, join=True)


@router.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery) -> None:
    _pending_joins.pop(call.data.split(":", 1)[1], None)
    await call.answer("Отменено")
    await _drop_markup(call)


@router.callback_query(F.data == "connect:saved")
async def cb_connect_saved(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    status = await call.message.answer("🔌 Подключаю сохранённые чаты…")
    try:
        res = await jobs.run_job(jobs.connect_saved_chats())
    except jobs.Busy as e:
        await status.edit_text(_busy(e))
        return
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ Не получилось: {fmt.esc(e)}")
        return
    text, markup = fmt.fmt_connect_result(res)
    await status.edit_text(text or "Подключать нечего.", reply_markup=markup)


@router.callback_query(F.data == "hist:all")
async def cb_history(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    ids = await jobs.chats_without_history()
    if not ids:
        await call.message.answer("Вся история уже загружена.")
        return
    await _load_history(call.message, ids)


@router.message(Command("models"))
async def cmd_models(message: Message, command: CommandObject) -> None:
    grep = (command.args or "").strip().lower()
    try:
        ids = await build_llm().list_models()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Шлюз не ответил: {type(e).__name__}: {fmt.esc(e)}")
        return
    shown = [m for m in ids if grep in m.lower()] if grep else ids
    head = f"<b>{fmt.esc(settings.llm_base_url)}</b> — моделей: {len(ids)}\n"
    if not shown:
        await message.answer(head + f"По «{fmt.esc(grep)}» ничего не найдено.")
        return
    body = "\n".join(
        f"<code>{fmt.esc(m)}</code>" + (" ← текущая" if m == settings.llm_model else "")
        for m in shown
    )
    await _reply_long(message, head + body)


# ------------------------------------------------------------ импорт файла


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    """Приём result.json из экспорта Telegram Desktop прямо в чат."""
    doc = message.document
    if doc is None:
        return
    if not (doc.file_name or "").lower().endswith(".json"):
        await message.answer(
            "Жду <code>result.json</code> из «Экспорт истории чата» "
            "в Telegram Desktop (формат JSON, не HTML)."
        )
        return
    if (doc.file_size or 0) > MAX_UPLOAD:
        size_mb = (doc.file_size or 0) / 1024 / 1024
        await message.answer(
            f"Файл {size_mb:.0f} МБ, а Telegram отдаёт ботам максимум 20 МБ.\n"
            "Варианты: при экспорте выбери период покороче (например, последние полгода), "
            "или залей через консоль: <code>chat-parser import-json путь\\к\\result.json</code>"
        )
        return

    status = await message.answer("⬇️ Скачиваю…")
    dest = Path(tempfile.gettempdir()) / f"tdesktop-{doc.file_unique_id}.json"
    try:
        await bot.download(doc, destination=dest)
        await status.edit_text("📖 Читаю переписку и делю её на обсуждения…")
        stats = await jobs.import_export(dest)
    except jobs.Busy as e:
        await status.edit_text(_busy(e))
        return
    except ValueError as e:
        await status.edit_text(f"❌ {fmt.esc(e)}")
        return
    except Exception as e:  # noqa: BLE001 — показать причину, не ронять бота
        await status.edit_text(f"❌ {type(e).__name__}: {fmt.esc(e)}")
        return
    finally:
        dest.unlink(missing_ok=True)

    thr = stats.get("threads") or {}
    dup = stats.get("duplicates", 0)
    await status.edit_text(
        f"✅ Загрузил чат <b>{fmt.esc(stats['title'])}</b>\n"
        f"Сообщений: {fmt.fmt_num(stats.get('in_file', 0))}"
        + (f" (новых: {fmt.fmt_num(stats['saved'])}, остальные уже были)" if dup else "")
        + f"\nОбсуждений в чате: {fmt.fmt_num(thr.get('threads', 0))}"
    )
    await _extract_menu(message)


# ------------------------------------------------ скрытая статистика расхода


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    """Расход токенов. Не в меню и не в справке — пользователю это не нужно."""
    pool = await db.get_pool()
    try:
        report = await usage.report(pool)
    except asyncpg.UndefinedTableError:
        await message.answer("Учёт расхода ещё не включён: перезапусти бота — схема обновится.")
        return
    await _reply_long(
        message, fmt.fmt_usage(report, settings.llm_price_in, settings.llm_price_out)
    )


# ------------------------------------------ скрытая: отслеживание и отчёт


@router.message(Command("live"))
async def cmd_live(message: Message) -> None:
    """Пауза отслеживания, расход за день и отчёт по запросу. Не в меню."""
    from . import live

    on = await live.is_enabled()
    lines = [f"📡 Отслеживание: <b>{'включено' if on else 'на паузе'}</b>"]
    if not settings.telegram_ready:
        lines.append("Нужны ключи Telegram API — без них читать чаты я не могу.")
    at = clock.hhmm(*await live.report_time())
    lines.append(
        f"Каждый день в {at} ({clock.tz_label()}) забираю переписку за сутки, разбираю "
        "её и присылаю итоги дня. Поменять время — кнопка ⏰ Время отчёта."
    )
    spent = await live.spent_today()
    if live.budget_enabled():
        lines.append(f"Потрачено сегодня: {spent:.2f} из {settings.live_daily_budget:g} ₽")
    hit = await live.budget_hit_today()
    if hit:
        lines.append(f"⚠️ Сегодня лимит исчерпан в {fmt.when(hit)}.")
    raw = await db.get_setting(live.LAST_RUN_KEY)
    if raw:
        from datetime import datetime

        lines.append(f"Последний разбор: {fmt.when(datetime.fromisoformat(raw))}")
    sent = await db.get_setting(live.REPORT_DATE_KEY) == clock.now().date().isoformat()
    lines.append(f"Следующие итоги: {fmt.when_next(await live.next_report_at())}"
                 + (" (сегодняшние уже отправлены)" if sent else ""))
    if settings.email_ready:
        lines.append("Итоги дня в PDF дублирую на почту: "
                     + fmt.esc(", ".join(settings.email_recipients)))
    else:
        lines.append("На почту не отправляю: в .env не заданы REPORT_EMAIL_TO, SMTP_USER и "
                     "SMTP_PASSWORD.")
    toggle = ("⏸ Поставить на паузу", "live:off") if on else ("▶️ Включить", "live:on")
    rows = [[toggle], [("📤 Итоги дня сейчас", "live:report")]]
    if settings.email_ready:
        rows.append([("✉️ Проверить почту", "live:mail")])
    await message.answer("\n".join(lines), reply_markup=_kb(*rows))


@router.message(F.text == BTN_SEND_NOW)
async def btn_send_now(message: Message, bot: Bot) -> None:
    """Временная кнопка для проверки: итоги за последние сутки прямо сейчас —
    в Telegram и на почту. Расписание не трогает."""
    from . import delivery, live

    head = "📤 Собираю итоги за последние сутки…"
    job_id, progress = await _start_live(message, head)
    task = asyncio.ensure_future(
        jobs.run_job(live.build_report(mark_sent=False, job_id=job_id))
    )
    try:
        shown = ""
        while not task.done():  # показываем, на каком шаге сбор
            await asyncio.wait({task}, timeout=3)
            job = jobs.current()
            if job and job["id"] == job_id and job.get("progress") not in ("", shown):
                shown = job["progress"]
                await progress.set(f"{head}\n{fmt.esc(shown)}")
        report = task.result()
    except jobs.Cancelled:
        await progress.set(fmt.fmt_stopped("сбор итогов"), final=True)
        return
    except jobs.Busy as e:
        await progress.set(_busy(e), final=True)
        return
    except Exception as e:  # noqa: BLE001
        await progress.set(f"❌ Не получилось собрать: {fmt.esc(e)}", final=True)
        return
    finally:
        if not task.done():
            task.cancel()
    emailed = await delivery.deliver(bot, [message.chat.id], report, email=True)
    note = "✅ Готово — итоги за последние сутки ниже."
    if emailed:
        note += " Копия ушла на почту: " + fmt.esc(", ".join(settings.email_recipients)) + "."
    elif not settings.email_ready:
        note += " На почту не отправлял: она не настроена."
    note += " Отчёт по расписанию придёт как обычно."
    await progress.set(note, final=True)


@router.callback_query(F.data == "live:mail")
async def cb_live_mail(call: CallbackQuery) -> None:
    from .. import mailer

    await call.answer()
    if call.message is None:
        return
    status = await call.message.answer("✉️ Отправляю тестовое письмо…")
    try:
        await mailer.send(
            "Проверка почты — мониторинг чатов оптики",
            "Почта настроена правильно: итоги дня в PDF будут приходить сюда.\n\n"
            "— chat-parser, мониторинг чатов рынка оптики",
        )
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ {fmt.esc(e)}")
        return
    await status.edit_text("✅ Письмо ушло: " + fmt.esc(", ".join(settings.email_recipients))
                           + ". Если его нет во «Входящих» — загляни в «Спам».")


@router.callback_query(F.data.in_({"live:on", "live:off"}))
async def cb_live_toggle(call: CallbackQuery) -> None:
    from . import live

    on = call.data == "live:on"
    await live.set_enabled(on)
    await call.answer("Включено" if on else "На паузе")
    await _drop_markup(call)
    if call.message is not None:
        await call.message.answer(
            "▶️ Отслеживание включено." if on else
            "⏸ Вечерний разбор на паузе: переписку я продолжаю собирать, но нейросеть "
            "не запускаю. Итоги дня придут без разбора. Включить — /live"
        )


@router.callback_query(F.data.in_({"live:report", "live:catchup"}))
async def cb_live_report(call: CallbackQuery, bot: Bot) -> None:
    """live:report — показать сейчас (по расписанию всё равно придут);
    live:catchup — сегодняшние, пропавшие из-за смены времени: их и на почту."""
    from . import delivery, live

    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    catch_up = call.data == "live:catchup"
    status = await call.message.answer("📤 Собираю итоги дня…")
    try:
        report = await jobs.run_job(live.build_report(mark_sent=catch_up, catch_up=catch_up))
    except jobs.Busy as e:
        await status.edit_text(_busy(e))
        return
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ Не получилось собрать: {fmt.esc(e)}")
        return
    await status.delete()
    await delivery.deliver(bot, [call.message.chat.id], report, email=catch_up)


# ------------------------------------------------------ участники чатов


async def _send_person(message: Message, label: str, prefix: str = "") -> None:
    pool = await db.get_pool()
    c = await people.card(pool, label)
    if c is None:
        await message.answer("Такого участника нет в базе.")
        return
    if c.get("hint_chat_id") and c.get("hint_message_id"):
        username = await pool.fetchval("select username from chats where id = $1",
                                       c["hint_chat_id"])
        c["hint_link"] = message_link(c["hint_chat_id"], username, c["hint_message_id"])
    code = label.removeprefix("u:")
    rows = [[("✏️ Компания и роль", f"pe:{code}"), ("📝 Заметка", f"pn:{code}")]]
    if c.get("company_hint") or c.get("role_hint"):
        rows.insert(0, [("✅ Верно, сохранить подсказку", f"pa:{code}")])
    await _reply_long(message, prefix + fmt.fmt_person(c), reply_markup=_kb(*rows))


TIME_PRESETS = ("08:00", "09:00", "12:00", "18:00", "20:00", "21:00", "22:00", "23:00")


@router.message(F.text == BTN_TIME)
@router.message(Command("time"))
async def cmd_time(message: Message) -> None:
    from . import live

    hour, minute = await live.report_time()
    current = f"{hour:02d}:{minute:02d}"
    nxt = await live.next_report_at()
    buttons = [(("✓ " if t == current else "") + clock.hhmm(int(t[:2]), int(t[3:])),
                "rt:" + t.replace(":", "")) for t in TIME_PRESETS]
    await message.answer(
        f"⏰ Итоги дня приходят в <b>{clock.hhmm(hour, minute)}</b> ({clock.tz_label()}), "
        f"следующие — {fmt.when_next(nxt)}.\n\nВо сколько присылать? Выбери или нажми "
        "«✍️ Другое время» и напиши своё, например 21:30.",
        reply_markup=_kb(buttons[:4], buttons[4:],
                         [("✍️ Другое время", "rt:custom"), ("Отмена", "cancel")]),
    )


@router.callback_query(F.data.startswith("rt:"))
async def cb_time(call: CallbackQuery) -> None:
    from . import live

    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    value = call.data.split(":", 1)[1]
    if value == "custom":
        await call.message.answer(
            f"{TIME_PROMPT}\n\nНапиши время по {clock.tz_label()}, например <b>21:30</b> "
            "или <b>9</b>.",
            reply_markup=CANCEL_KB,
        )
        _await_answer(call.from_user, "time")
        return
    parsed = live.parse_time(value[:2] + ":" + value[2:])
    if parsed:
        await _apply_time(call.message, *parsed)


async def _apply_time(message: Message, hour: int, minute: int) -> None:
    from . import live

    nxt, skipped = await live.set_report_time(hour, minute)
    at = clock.hhmm(hour, minute)
    text = (f"✅ Итоги дня теперь приходят в <b>{at}</b> ({clock.tz_label()}). "
            f"Следующие — {fmt.when_next(nxt)}.")
    if skipped:
        await message.answer(
            text + f"\n\nВремя {at} сегодня уже прошло, поэтому сегодняшних итогов по "
            "расписанию не будет. Прислать их сейчас?",
            reply_markup=_kb([("📤 Прислать итоги за сегодня", "live:catchup")]),
        )
    else:
        await message.answer(text)


@router.message(Command(re.compile(r"who_([0-9a-f]{8})")))
async def cmd_who(message: Message, command: CommandObject) -> None:
    label = "u:" + command.regexp_match.group(1)
    if (command.args or "").strip():  # «/who_1a2b3c4d Оптика Люкс, владелец»
        await _save_person(message, label, command.args)
    else:
        await _send_person(message, label)


@router.message(F.text == BTN_PERSON)
async def btn_person(message: Message) -> None:
    await message.answer(
        f"{PERSON_PROMPT}\n\nПришли одним сообщением @username, а дальше через запятую — "
        f"компания, роль и заметка, что знаешь:\n{PERSON_EXAMPLES}\n\n"
        "Можно по строкам: первая — @username, потом компания, роль, заметка. "
        "Вместо @username подойдёт /who_… из подписи под цитатой. "
        "Не присланное не меняется.",
        reply_markup=CANCEL_KB,
    )
    _await_answer(message.from_user, "person")


# «@ivan_optika Оптика Люкс, владелец» и без ответа на подсказку. Один
# @ник без текста — это добавление чата (LINK_FILTER).
@router.message(F.text.regexp(r"^\s*@[A-Za-z][A-Za-z0-9_]{3,31}[\s,:;—–-]+\S"))
async def on_person_line(message: Message) -> None:
    await _person_info(message, message.text or "")


async def _person_info(message: Message, text: str) -> None:
    parsed = people.parse_person_info(text)
    if parsed is None:
        await message.answer(
            f"{PERSON_PROMPT}\n\nНе вижу @username в начале. Напиши так:\n"
            f"{PERSON_EXAMPLES}\n\nЕсли ника нет — открой карточку человека через /who_… "
            "под его цитатой или в 👥 списке участников чата.",
            reply_markup=CANCEL_KB,
        )
        _await_answer(message.from_user, "person")
        return
    username, label, rest = parsed
    if label is None:
        try:
            label = await jobs.find_person(username)
        except jobs.Busy as e:
            await message.answer(_busy(e) + "\nИскать человека в Telegram смогу, когда закончу.")
            return
        except jobs.NotAPerson:
            await message.answer(f"@{fmt.esc(username)} — это чат или канал, а не человек.")
            return
        except Exception as e:  # noqa: BLE001 — сеть, FloodWait
            await message.answer(f"❌ Не получилось найти @{fmt.esc(username)}: {fmt.esc(e)}")
            return
        if label is None:
            where = ("ни среди участников чатов, ни в Telegram" if settings.telegram_ready
                     else "среди участников подключённых чатов")
            await message.answer(f"🤔 Не нашёл @{fmt.esc(username)} {where}. Проверь ник.")
            return
    await _save_person(message, label, rest)


async def _save_person(message: Message, label: str, rest: str) -> None:
    company, role, note = people.parse_fields(rest)
    if not (company or role or note):
        await _send_person(message, label)
        return
    if not await people.update_info(await db.get_pool(), label, company, role, note):
        await message.answer("Такого участника нет в базе.")
        return
    await _send_person(message, label, prefix="✅ Сохранил.\n\n")


@router.callback_query(F.data.startswith("pe:") | F.data.startswith("pn:"))
async def cb_person_edit(call: CallbackQuery) -> None:
    await call.answer()
    if call.message is None:
        return
    kind, code = call.data.split(":", 1)
    label = "u:" + code
    pool = await db.get_pool()
    name = await pool.fetchval("select name from authors where author_label = $1", label)
    who = f" ({fmt.esc(name)})" if name else ""
    if kind == "pe":
        text = (f"✏️ Компания и роль для {label}{who}\n\nНапиши одной строкой через "
                "запятую, например: <i>Оптика Люкс, владелец</i>\n"
                "Только компания — без запятой. Стереть — «-».")
    else:
        text = f"📝 Заметка для {label}{who}\n\nНапиши текст заметки. Стереть — «-»."
    await call.message.answer(text, reply_markup=CANCEL_KB)
    _await_answer(call.from_user, ("edit:" if kind == "pe" else "note:") + label)


@router.callback_query(F.data.startswith("pa:"))
async def cb_person_accept(call: CallbackQuery) -> None:
    label = "u:" + call.data.split(":", 1)[1]
    ok = await people.accept_hint(await db.get_pool(), label)
    await call.answer("Сохранил" if ok else "Подсказки уже нет")
    await _drop_markup(call)
    if call.message is not None and ok:
        await _send_person(call.message, label, prefix="✅ Сохранил.\n\n")


@router.message(Command("people"))
async def cmd_people(message: Message) -> None:
    rows = await people.listing(await db.get_pool(), None)
    await _reply_long(message, fmt.fmt_people(rows, "все чаты"))


@router.callback_query(F.data.startswith("ppl:"))
async def cb_people_in_chat(call: CallbackQuery) -> None:
    await call.answer()
    if call.message is None:
        return
    chat_id = int(call.data.split(":", 1)[1])
    pool = await db.get_pool()
    title = await pool.fetchval("select title from chats where id = $1", chat_id)
    if title is None:
        await call.message.answer("Этого чата уже нет в списке.")
        return
    rows = await people.listing(pool, chat_id)
    kb = None
    if settings.telegram_ready:
        kb = _kb([("🔄 Подтянуть имена из Telegram", f"names:{chat_id}")])
    text = fmt.fmt_people(rows, title)
    if kb:
        await _reply_long(call.message, text, reply_markup=kb)
    else:
        await _reply_long(call.message, text)


@router.callback_query(F.data.startswith("names:"))
async def cb_refresh_names(call: CallbackQuery) -> None:
    await call.answer()
    await _drop_markup(call)
    if call.message is None:
        return
    status = await call.message.answer("🔄 Подтягиваю имена участников из Telegram…")
    try:
        n = await jobs.run_job(jobs.refresh_names(int(call.data.split(":", 1)[1])))
    except jobs.Busy as e:
        await status.edit_text(_busy(e))
        return
    except Exception as e:  # noqa: BLE001 — например, админ скрыл список участников
        await status.edit_text(
            f"❌ Не получилось: {fmt.esc(e)}\nВ некоторых чатах админы скрывают список "
            "участников — тогда имена появятся, когда люди будут писать."
        )
        return
    await status.edit_text(f"✅ Обновил имена: {n}. Открой список участников ещё раз.")
