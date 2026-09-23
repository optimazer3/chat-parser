from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import db
from ..config import settings
from ..ingest import collector, tdesktop
from ..ingest.client import build_client
from ..llm import build_llm
from . import format as fmt
from . import jobs

router = Router()

# Bot API отдаёт боту файлы не больше 20 МБ. Экспорт живого чата легко больше.
MAX_UPLOAD = 20 * 1024 * 1024

HELP = """<b>Мониторинг чатов рынка оптики</b>

/status — что собрано и что в очереди
/chats — список чатов
/addchat &lt;ссылка&gt; — добавить чат (приватный спросит подтверждение)
/run — прогнать весь цикл сейчас
/top [N] — топ болей
/pain &lt;номер&gt; — карточка боли
/signals [N] — последние сигналы (для калибровки промпта)
/report — прислать отчёт файлом
/models [фильтр] — модели, доступные на шлюзе

Нет API-ключей Telegram? Пришли сюда файлом <code>result.json</code> из
«Экспорт истории чата» в Telegram Desktop — залью вручную.
"""

# Токен -> ссылка: callback_data ограничен 64 байтами, ссылку туда не засунуть.
_pending_joins: dict[str, str] = {}


async def _reply_long(message: Message, text: str) -> None:
    for part in fmt.chunks(text):
        await message.answer(part)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("status"))
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
    pending = await pool.fetchval("select count(*) from threads where status = 'pending'")
    text = fmt.fmt_status(list(chats), pending, await jobs.last_pipeline_at())
    if jobs.is_running():
        text += "\n\n⏳ Прогон идёт прямо сейчас."
    await _reply_long(message, text)


@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    pool = await db.get_pool()
    rows = await pool.fetch(
        "select id, title, username, is_active, audience_hint from chats order by id"
    )
    if not rows:
        await message.answer("Чатов нет. Добавь: /addchat &lt;ссылка&gt;")
        return
    lines = []
    for r in rows:
        mark = "" if r["is_active"] else " (выключен)"
        handle = f" @{r['username']}" if r["username"] else ""
        hint = f" · {r['audience_hint']}" if r["audience_hint"] else ""
        lines.append(f"<code>{r['id']}</code> <b>{fmt.esc(r['title'])}</b>{handle}{hint}{mark}")
    await _reply_long(message, "\n".join(lines))


@router.message(Command("addchat"))
async def cmd_addchat(message: Message, command: CommandObject) -> None:
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Формат: /addchat @chat или /addchat https://t.me/+HASH")
        return
    await _add_chat(message, ref, join=False)


async def _add_chat(message: Message, ref: str, join: bool) -> None:
    pool = await db.get_pool()
    client = build_client()
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
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Вступить и добавить", callback_data=f"join:{token}")],
                [InlineKeyboardButton(text="Отмена", callback_data=f"drop:{token}")],
            ]
        )
        await message.answer(
            f"{fmt.esc(e)}\n\n⚠️ Вступление — самое рискованное для аккаунта действие. "
            "Держись в пределах 5–10 вступлений в сутки.",
            reply_markup=kb,
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
    await call.answer()
    if not ref or call.message is None:
        await call.answer("Запрос устарел, повтори /addchat", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Вступаю…")
    await _add_chat(call.message, ref, join=True)


@router.callback_query(F.data.startswith("drop:"))
async def cb_drop(call: CallbackQuery) -> None:
    _pending_joins.pop(call.data.split(":", 1)[1], None)
    await call.answer("Отменено")
    if call.message:
        await call.message.edit_reply_markup(reply_markup=None)


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    status = await message.answer("Запускаю…")

    async def progress(text: str) -> None:
        await status.edit_text(text)

    since = await jobs.last_pipeline_at()
    try:
        stats = await jobs.run_pipeline(progress)
    except jobs.Busy:
        await status.edit_text("⏳ Прогон уже идёт, дождись окончания.")
        return
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ Прогон упал: {type(e).__name__}: {fmt.esc(e)}")
        return

    new_msgs, new_signals = await jobs.since_counts(since)
    pool = await db.get_pool()
    top = await pool.fetch(
        "select id, label, score, n_authors from clusters order by score desc limit 5"
    )
    await status.edit_text("✅ Готово")
    await _reply_long(message, fmt.fmt_digest(stats, new_signals, new_msgs, list(top)))
    await _send_report(message)


@router.message(Command("top"))
async def cmd_top(message: Message, command: CommandObject) -> None:
    limit = 10
    if command.args and command.args.strip().isdigit():
        limit = min(int(command.args.strip()), 30)
    pool = await db.get_pool()
    rows = await pool.fetch(
        """
        select id, audience, label, score, n_authors, n_chats, n_signals
          from clusters order by audience, score desc limit $1
        """,
        limit,
    )
    await _reply_long(message, fmt.fmt_top(list(rows)))


@router.message(Command("pain"))
async def cmd_pain(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /pain 12 (номер из /top)")
        return
    pool = await db.get_pool()
    cluster = await pool.fetchrow("select * from clusters where id = $1", int(arg))
    if cluster is None:
        await message.answer("Такой боли нет. Список: /top")
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
        await message.answer("Отчёта ещё нет. Сначала /run.")
        return
    await message.answer_document(FSInputFile(jobs.REPORT_PATH), caption="Полный отчёт")


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    await _send_report(message)


@router.message(Command("models"))
async def cmd_models(message: Message, command: CommandObject) -> None:
    grep = (command.args or "").strip().lower()
    try:
        ids = await build_llm().list_models()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Шлюз не ответил: {type(e).__name__}: {fmt.esc(e)}")
        return
    shown = [m for m in ids if grep in m.lower()] if grep else ids
    head = f"<b>{settings.llm_base_url}</b> — моделей: {len(ids)}\n"
    if not shown:
        await message.answer(head + f"По «{fmt.esc(grep)}» ничего не найдено.")
        return
    body = "\n".join(
        f"<code>{fmt.esc(m)}</code>" + (" ← текущая" if m == settings.llm_model else "")
        for m in shown
    )
    await _reply_long(message, head + body)


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    """Приём result.json из экспорта Telegram Desktop прямо в чат."""
    doc = message.document
    if doc is None:
        return
    if not (doc.file_name or "").lower().endswith(".json"):
        await message.answer(
            "Жду <code>result.json</code> из «Экспорт истории чата» "
            "в Telegram Desktop (формат JSON)."
        )
        return
    if (doc.file_size or 0) > MAX_UPLOAD:
        size_mb = (doc.file_size or 0) / 1024 / 1024
        await message.answer(
            f"Файл {size_mb:.0f} МБ, а Telegram отдаёт ботам максимум 20 МБ.\n"
            "Залей через консоль:\n<code>chat-parser import-json путь\\к\\result.json</code>"
        )
        return

    status = await message.answer("⬇️ Скачиваю…")
    dest = Path(tempfile.gettempdir()) / f"tdesktop-{doc.file_unique_id}.json"
    try:
        await bot.download(doc, destination=dest)
        await status.edit_text("📖 Разбираю файл…")
        stats = await tdesktop.import_file(await db.get_pool(), dest)
    except ValueError as e:
        await status.edit_text(f"❌ {fmt.esc(e)}")
        return
    except Exception as e:  # noqa: BLE001 — показать причину, не ронять бота
        await status.edit_text(f"❌ {type(e).__name__}: {fmt.esc(e)}")
        return
    finally:
        dest.unlink(missing_ok=True)

    await status.edit_text(
        f"✅ <b>{fmt.esc(stats['title'])}</b>\n"
        f"в файле: {stats.get('in_file', 0)} · добавлено: {stats['saved']} · "
        f"уже были: {stats.get('duplicates', 0)} · служебных пропущено: {stats['skipped']}\n\n"
        "Дальше: /run"
    )
