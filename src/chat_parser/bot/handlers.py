"""Команды и кнопки бота.

Всё, что делается из консоли, доступно отсюда. Дорогие операции (разбор
сигналов моделью) никогда не запускаются на всю очередь без явного выбора,
а любую долгую операцию можно остановить кнопкой ⏹. Расход токенов
пользователю не показывается — только по скрытой команде /usage.
"""

from __future__ import annotations

import re
import tempfile
import time
import uuid
from pathlib import Path

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .. import db, usage
from ..config import settings
from ..ingest import collector
from ..ingest.client import build_client
from ..llm import build_llm
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

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_EXTRACT)],
        [KeyboardButton(text=BTN_CLUSTER), KeyboardButton(text=BTN_TOP)],
        [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

HELP = """👋 <b>Я нахожу боли и потребности рынка оптики</b>

Читаю переписку в чатах владельцев оптик, продавцов, врачей и покупателей
и выписываю, на что люди жалуются, что ищут и каких решений им не хватает.
Потом собираю это в список «болей» — от самых частых и острых к редким.

<b>Как это работает</b>
1️⃣ Ты присылаешь мне историю чата — файлом.
2️⃣ Я делю переписку на <b>обсуждения</b>: вопрос и ответы на него.
3️⃣ Нейросеть читает каждое обсуждение и выписывает <b>сигналы</b> — жалобы,
запросы, вопросы, обходные пути. Каждый сигнал подтверждён дословной
цитатой: если цитаты в переписке нет, сигнал выбрасывается.
4️⃣ Похожие сигналы я собираю в <b>боли</b> и сортирую: сколько разных людей об
этом говорят, в скольких чатах, насколько остро.

<b>С чего начать</b>
1. В Telegram Desktop на компьютере открой нужный чат → ⋮ справа вверху →
   «Экспорт истории чата».
2. Формат — <b>JSON</b> (не HTML!). Фото и видео сними — нужен только текст.
3. Пришли мне получившийся файл <code>result.json</code>.
4. Я спрошу, сколько обсуждений разобрать. Для начала выбери <b>20</b> —
   это быстро и покажет, как всё работает.
5. Нажми 🧩 <b>Пересчитать боли</b>, затем 🔝 <b>Топ болей</b>.

<b>Кнопки внизу</b>
📊 <b>Статус</b> — что загружено, сколько ждёт разбора, что выполняется
🧠 <b>Разобрать</b> — отдать обсуждения нейросети (спрошу, сколько)
🧩 <b>Пересчитать боли</b> — собрать сигналы в боли и описать каждую
🔝 <b>Топ болей</b> — главные боли по группам людей
📄 <b>Отчёт</b> — всё одним файлом
❓ <b>Помощь</b> — это сообщение

<b>Полезно знать</b>
• Любую долгую операцию можно остановить кнопкой ⏹ — сделанное сохранится.
• Меню можно закрыть кнопкой «Отмена» — ничего не запустится.
• Раз в сутки я сам проверяю новые данные и присылаю сводку.
• В топе рядом с каждой болью есть ссылка /pain_… — нажми, пришлю
  подробности: кто страдает, как выкручиваются, цитаты, идеи решений.

<b>Ещё команды</b>
/signals — последние найденные сигналы
/retry — повторить обсуждения, которые не получилось разобрать
/redo — разобрать всё заново (после изменения настроек разбора)
/run — всё за один раз: разбор, боли и отчёт
/chats — какие чаты загружены
"""

# Токен -> ссылка: callback_data ограничен 64 байтами, ссылку туда не засунуть.
_pending_joins: dict[str, str] = {}


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


@router.message(CommandStart())
@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=MAIN_KB)


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
        # Кнопка и здесь: у ночного прогона нет своего сообщения с прогрессом.
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
    quotes = await pool.fetch(
        "select evidence_quote from signals where cluster_id = $1 "
        "order by intensity desc limit 4",
        cluster["id"],
    )
    await _reply_long(message, fmt.fmt_card(cluster, list(quotes)))


@router.message(Command("signals"))
async def cmd_signals(message: Message, command: CommandObject) -> None:
    limit = 10
    if command.args and command.args.strip().isdigit():
        limit = min(int(command.args.strip()), 30)
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select type, audience, summary, evidence_quote, intensity
          from signals order by id desc limit $1
        """,
        limit,
    )
    await _reply_long(message, fmt.fmt_signals(list(rows)))


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


@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select c.id, c.title, c.username, c.is_active, c.audience_hint,
               (select count(*) from messages m where m.chat_id = c.id) msgs
          from chats c order by c.id
        """
    )
    if not rows:
        await message.answer("Чатов нет. Пришли сюда файлом result.json из Telegram Desktop.")
        return
    lines = []
    for r in rows:
        mark = "" if r["is_active"] else " (выключен)"
        handle = f" @{r['username']}" if r["username"] else ""
        hint = f" · {r['audience_hint']}" if r["audience_hint"] else ""
        lines.append(
            f"<b>{fmt.esc(r['title'])}</b>{handle}{hint}{mark}\n"
            f"   <code>{r['id']}</code> · {r['msgs']} сообщ."
        )
    await _reply_long(message, "\n".join(lines))


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, command: CommandObject) -> None:
    if not settings.telegram_ready:
        await message.answer(
            "Автоматическая выгрузка выключена: в .env не заданы TG_API_ID и TG_API_HASH.\n\n"
            "Пока без них — пришли сюда файлом <code>result.json</code>: "
            "Telegram Desktop → чат → ⋮ → Экспорт истории чата → формат JSON."
        )
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Формат: /addchat @chat или /addchat https://t.me/+HASH")
        return
    await _add_chat(message, ref, join=False)


async def _add_chat(message: Message, ref: str, join: bool) -> None:
    pool = await db.get_pool()
    try:
        client = build_client()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ {fmt.esc(e)}")
        return
    await client.start()
    try:
        chat_id = await collector.register_chat(client, pool, ref, join=join)
        title = await pool.fetchval("select title from chats where id = $1", chat_id)
        await message.answer(
            f"✅ Добавлен <b>{fmt.esc(title)}</b> (<code>{chat_id}</code>)\n"
            "Первая выгрузка: /run"
        )
    except collector.NeedsJoin as e:
        token = uuid.uuid4().hex[:8]
        _pending_joins[token] = ref
        await message.answer(
            f"{fmt.esc(e)}\n\n⚠️ Вступление — самое рискованное для аккаунта действие. "
            "Держись в пределах 5–10 вступлений в сутки.",
            reply_markup=_kb(
                [("Вступить и добавить", f"join:{token}")], [("Отмена", f"drop:{token}")]
            ),
        )
    except collector.JoinPending as e:
        await message.answer(f"⏳ {fmt.esc(e)}")
    except Exception as e:  # noqa: BLE001 — показать причину, не падать
        await message.answer(f"❌ {type(e).__name__}: {fmt.esc(e)}")
    finally:
        await client.disconnect()


@router.callback_query(F.data.startswith("join:"))
async def cb_join(call: CallbackQuery) -> None:
    token = call.data.split(":", 1)[1]
    ref = _pending_joins.pop(token, None)
    if not ref or call.message is None:
        await call.answer("Запрос устарел, повтори /addchat", show_alert=True)
        return
    await call.answer()
    await _drop_markup(call)
    await call.message.answer("Вступаю…")
    await _add_chat(call.message, ref, join=True)


@router.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery) -> None:
    _pending_joins.pop(call.data.split(":", 1)[1], None)
    await call.answer("Отменено")
    await _drop_markup(call)


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
