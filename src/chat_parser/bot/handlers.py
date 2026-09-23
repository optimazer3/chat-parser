"""Команды и кнопки бота.

Всё, что делается из консоли, доступно отсюда. Дорогие операции (разбор
сигналов LLM'ом) никогда не запускаются на всю очередь без явного выбора:
бот показывает размер очереди и оценку токенов и спрашивает, сколько брать.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from pathlib import Path

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

from .. import db
from ..analyze import extract as extract_mod
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

HELP = """<b>Мониторинг чатов рынка оптики</b>

<b>Как пользоваться</b>
1. Пришли сюда файлом <code>result.json</code> — экспорт чата из Telegram Desktop
   (⋮ → Экспорт истории чата → формат JSON).
2. Бот зальёт историю и предложит разобрать сигналы. Начни с 20 тредов —
   после пробы он посчитает, сколько токенов уйдёт на всё.
3. 🧩 Пересчитать боли — сгруппировать сигналы и написать карточки.
4. 🔝 Топ болей, /pain &lt;номер&gt;, 📄 Отчёт — смотреть результат.

<b>Команды</b>
/status — что собрано, что в очереди, что сейчас выполняется
/extract [N] — разобрать N тредов (без числа — меню с оценкой)
/cluster — сгруппировать сигналы в боли и написать карточки
/top [N] — топ болей
/pain &lt;номер&gt; — карточка боли
/signals [N] — последние сигналы
/report — отчёт файлом
/redo — переразобрать уже разобранное (после правки промпта)
/retry — повторить треды, упавшие с ошибкой
/run — полный цикл одной кнопкой
/chats, /addchat, /models — чаты и модели
"""

# Токен -> ссылка: callback_data ограничен 64 байтами, ссылку туда не засунуть.
_pending_joins: dict[str, str] = {}


class LiveMessage:
    """Сообщение-прогресс, которое правится не чаще раза в interval секунд.

    Telegram ограничивает частоту правок: обновлять на каждом треде — быстрый
    путь к 429 и к тому, что прогресс вообще перестанет показываться.
    """

    def __init__(self, message: Message, interval: float = 3.0) -> None:
        self.message = message
        self.interval = interval
        self._last = 0.0
        self._shown = message.text or ""

    async def set(self, text: str, force: bool = False) -> None:
        if text == self._shown:
            return
        now = time.monotonic()
        if not force and now - self._last < self.interval:
            return
        try:
            await self.message.edit_text(text)
        except TelegramAPIError:
            # Итог терять нельзя: если правка не прошла, шлём отдельным сообщением.
            if force:
                await self.message.answer(text)
            return
        self._shown = text
        self._last = now


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
        text += f" · упавших тредов: {counts['f']} (/retry)"
    job = jobs.current()
    if job:
        progress = f": {fmt.esc(job['progress'])}" if job["progress"] else ""
        text += f"\n\n⏳ Сейчас идёт «{fmt.esc(job['name'])}»{progress}"
    await _reply_long(message, text)


# ------------------------------------------------------------- разбор сигналов


async def _extract_menu(message: Message, prefix: str = "") -> None:
    pending = await jobs.queue_size()
    text = prefix + fmt.fmt_estimate(pending, await jobs.tokens_per_thread())
    choices = fmt.extract_choices(pending)
    if not choices:
        await message.answer(text)
        return
    await message.answer(
        text + "\n\nСколько разобрать?",
        reply_markup=_kb([(label, f"ex:{value}") for label, value in choices]),
    )


async def _do_extract(message: Message, limit: int | None) -> None:
    live = LiveMessage(await message.answer("🧠 Запускаю разбор…"))
    try:
        stats = await jobs.run_extract(
            lambda done, total, st: live.set(fmt.fmt_extract_progress(done, total, st)),
            limit,
        )
    except jobs.Busy as e:
        await live.set(_busy(e), force=True)
        return
    except Exception as e:  # noqa: BLE001 — показать причину, не ронять бота
        await live.set(f"❌ Разбор упал: {type(e).__name__}: {fmt.esc(e)}", force=True)
        return

    lines = extract_mod.summary_lines(stats, retry_hint="/retry")
    await live.set(fmt.fmt_extract_summary(lines), force=True)
    nxt = []
    if stats.get("pending_left"):
        nxt.append(("🧠 Ещё 20", "ex:20"))
    nxt.append(("🧩 Пересчитать боли", "cl"))
    retry = [("🔁 Повторить упавшие", "retry")] if stats["failed"] else []
    await message.answer("Что дальше?", reply_markup=_kb(nxt, retry))


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
        await message.answer("Переразбирать нечего: разобранных тредов нет.")
        return
    await message.answer(
        f"Вернуть в очередь <b>{n}</b> уже разобранных тредов?\n"
        "Их сигналы перезапишутся при следующем разборе — дублей не будет. "
        "Нужно после правки промпта.",
        reply_markup=_kb([(f"Да, вернуть {n}", "redo:yes"), ("Отмена", "cancel")]),
    )


async def _reset_and_menu(message: Message, kind: str) -> None:
    try:
        n = await jobs.reset_queue(kind)
    except jobs.Busy as e:
        await message.answer(_busy(e))
        return
    if not n:
        await message.answer("Упавших тредов нет." if kind == "failed" else "Возвращать нечего.")
        return
    what = "упавших " if kind == "failed" else ""
    await _extract_menu(message, prefix=f"Вернул в очередь {what}{fmt.threads_word(n)}.\n")


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
    live = LiveMessage(await message.answer("🧩 Запускаю группировку…"))
    try:
        stats = await jobs.run_cluster(lambda text: live.set(text))
    except jobs.Busy as e:
        await live.set(_busy(e), force=True)
        return
    except Exception as e:  # noqa: BLE001
        await live.set(f"❌ Группировка упала: {type(e).__name__}: {fmt.esc(e)}", force=True)
        return
    if not stats["clusters_total"]:
        await live.set(
            "Группировать пока нечего: нужно хотя бы 4 сигнала одной аудитории "
            "и чтобы они повторялись. Разбери больше тредов — 🧠 Разобрать.",
            force=True,
        )
        return
    await live.set(
        f"✅ Болей: <b>{stats['clusters_total']}</b>, карточек: {stats['cards']}. "
        "Отчёт обновлён.",
        force=True,
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


@router.message(Command("pain"))
async def cmd_pain(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Формат: /pain 12 (номер из 🔝 Топ болей)")
        return
    pool = await db.get_pool()
    cluster = await pool.fetchrow("select * from clusters where id = $1", int(arg))
    if cluster is None:
        await message.answer("Такой боли нет. Список: 🔝 Топ болей")
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
    live = LiveMessage(await message.answer("Запускаю полный цикл…"))
    since = await jobs.last_pipeline_at()
    try:
        stats = await jobs.run_pipeline(lambda text: live.set(text), extract_limit)
    except jobs.Busy as e:
        await live.set(_busy(e), force=True)
        return
    except Exception as e:  # noqa: BLE001
        await live.set(f"❌ Прогон упал: {type(e).__name__}: {fmt.esc(e)}", force=True)
        return

    new_msgs, new_signals = await jobs.since_counts(since)
    pool = await db.get_pool()
    top = await pool.fetch(
        "select id, label, score, n_authors from clusters order by score desc limit 5"
    )
    await live.set("✅ Готово", force=True)
    await _reply_long(message, fmt.fmt_digest(stats, new_signals, new_msgs, list(top)))
    await _send_report(message)


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    pending = await jobs.queue_size()
    if pending > settings.bot_confirm_threshold:
        estimate = fmt.fmt_estimate(pending, await jobs.tokens_per_thread())
        await message.answer(
            f"{estimate}\n\nПолный цикл разберёт всю очередь. Точно?",
            reply_markup=_kb(
                [(f"Всё: {pending}", "run:all"), ("Только 20", "run:20")],
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
            "Варианты: при экспорте выбери период покороче (например, последний год), "
            "или залей через консоль: <code>chat-parser import-json путь\\к\\result.json</code>"
        )
        return

    status = await message.answer("⬇️ Скачиваю…")
    dest = Path(tempfile.gettempdir()) / f"tdesktop-{doc.file_unique_id}.json"
    try:
        await bot.download(doc, destination=dest)
        await status.edit_text("📖 Разбираю файл и собираю диалоги…")
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
    await status.edit_text(
        f"✅ <b>{fmt.esc(stats['title'])}</b>\n"
        f"в файле: {stats.get('in_file', 0)} · добавлено: {stats['saved']} · "
        f"уже были: {stats.get('duplicates', 0)} · служебных пропущено: {stats['skipped']}\n"
        f"диалогов собрано: {thr.get('threads', 0)}"
    )
    await _extract_menu(message)
