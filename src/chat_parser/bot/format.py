"""Рендер сообщений бота. Telegram режет сообщения на 4096 символах."""

from __future__ import annotations

import html
import json
from typing import Any

from .. import clock, people

LIMIT = 3800

AUDIENCE_RU = {
    "owner": "владельцы оптик",
    "staff": "персонал салонов",
    "optometrist": "оптометристы",
    "supplier": "поставщики",
    "customer": "покупатели",
    "unknown": "не определено",
}


def when(ts: Any, with_year: bool = False) -> str:
    """Время в часовом поясе пользователя: «23.09 в 18:01». В базе всё в UTC."""
    local = ts.astimezone(clock.tz())
    return local.strftime("%d.%m.%Y в %H:%M" if with_year else "%d.%m в %H:%M")


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(3, "обсуждение", "обсуждения", "обсуждений") -> "обсуждения"."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def discussions(n: int) -> str:
    """«Обсуждение» — единица разбора: вопрос и ответы на него (в коде — thread)."""
    return f"{fmt_num(n)} {plural(n, 'обсуждение', 'обсуждения', 'обсуждений')}"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Режет по строкам, чтобы не рвать разметку посреди тега."""
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


def fmt_status(chats: list[Any], pending: int, last_run: Any) -> str:
    lines = ["<b>📊 Состояние</b>", ""]
    if not chats:
        lines.append("Чатов пока нет. Пришли мне файл result.json — как его получить, в ❓ Помощь.")
    for c in chats:
        flag = "✅" if c["backfill_done"] else "⏳"
        updated = f"обновлён {when(c['last_run'])}" if c["last_run"] else "ещё не загружен"
        lines.append(f"{flag} <b>{esc(c['title'])}</b>")
        lines.append(f"   {messages_word(c['msgs'])} · {updated}")
        if c["retry_after"]:
            lines.append(f"   ⛔ Telegram попросил паузу до {when(c['retry_after'])}")
    lines += ["", f"Ждут разбора: <b>{discussions(pending)}</b>"]
    if last_run:
        lines.append(f"Последнее полное обновление: {when(last_run)}")
    return "\n".join(lines)


def messages_word(n: int) -> str:
    return f"{fmt_num(n)} {plural(n, 'сообщение', 'сообщения', 'сообщений')}"


def people_word(n: int) -> str:
    return f"{n} {plural(n, 'человек', 'человека', 'человек')}"


def chats_word(n: int) -> str:
    return f"{n} {plural(n, 'чате', 'чатах', 'чатах')}"


def signals_word(n: int) -> str:
    return f"{n} {plural(n, 'сигнал', 'сигнала', 'сигналов')}"


def fmt_top(clusters: list[Any], title: str = "🔝 <b>Топ болей</b>") -> str:
    if not clusters:
        return (
            "Болей пока нет. Сначала разбери обсуждения — 🧠 Разобрать, "
            "потом собери их — 🧩 Пересчитать боли."
        )
    lines = [title]
    current = None
    for c in clusters:
        if c["audience"] != current:
            current = c["audience"]
            lines.append(f"\n<b>{AUDIENCE_RU.get(current, current).capitalize()}</b>")
        lines.append(
            f"• {esc(c['label'])}\n"
            f"   говорят {people_word(c['n_authors'])} в {chats_word(c['n_chats'])}, "
            f"{signals_word(c['n_signals'])} → /pain_{c['id']}"
        )
    lines.append("\nНажми на /pain_… рядом с болью — пришлю подробности и цитаты.")
    return "\n".join(lines)


TYPE_RU = {
    "pain": "боль",
    "need": "потребность",
    "jtbd": "задача",
    "question": "вопрос",
    "workaround": "обходной путь",
    "alternative": "чем пользуются",
    "willingness_to_pay": "про деньги",
    "feature_request": "хотят функцию",
}


def fmt_quote(q: Any) -> str:
    """Цитата, под ней — кто сказал (с карточкой участника) и ссылка на сообщение."""
    text = f"<blockquote>{esc(q['evidence_quote'])}</blockquote>"
    if not isinstance(q, dict):
        return text
    parts = []
    label = q.get("author_label")
    if label:
        who = people.display(q.get("a_name"), label, q.get("a_company"), q.get("a_role"))
        parts.append(f"— {esc(who)} {people.who_command(label)}")
    if q.get("link"):
        parts.append(f'<a href="{html.escape(q["link"], quote=True)}">↗ сообщение в чате</a>')
    if parts:
        text += "\n" + " · ".join(parts)
    return text


def fmt_card(cluster: Any, quotes: list[Any]) -> str:
    lines = [
        f"<b>{esc(cluster['label'])}</b>",
        f"<i>{AUDIENCE_RU.get(cluster['audience'], cluster['audience'])}</i>",
        "",
        f"<blockquote>{esc(cluster['statement'])}</blockquote>",
        "",
        f"Говорят {people_word(cluster['n_authors'])} в {chats_word(cluster['n_chats'])}, "
        f"{signals_word(cluster['n_signals'])}.",
    ]
    card = cluster["card"]
    if card:
        card = json.loads(card) if isinstance(card, str) else card
        lines += ["", f"<b>Кто:</b> {esc(card['who'])}", f"<b>Когда:</b> {esc(card['when'])}"]
        if card.get("current_workarounds"):
            lines.append("\n<b>Как выкручиваются:</b>")
            lines += [f"• {esc(w)}" for w in card["current_workarounds"]]
    if quotes:
        lines.append("\n<b>Цитаты:</b>")
        lines += [fmt_quote(q) for q in quotes]
    if card:
        if card.get("product_hypotheses"):
            lines.append("\n<b>Гипотезы:</b>")
            lines += [f"• {esc(h)}" for h in card["product_hypotheses"]]
        if card.get("open_questions"):
            lines.append("\n<b>Выяснить:</b>")
            lines += [f"• {esc(q)}" for q in card["open_questions"]]
    return "\n".join(lines)


def fmt_signals(rows: list[Any]) -> str:
    if not rows:
        return "Сигналов пока нет."
    lines = ["<b>Последние сигналы</b>", ""]
    for r in rows:
        kind = TYPE_RU.get(r["type"], r["type"])
        lines.append(
            f"<b>{esc(kind)}</b> · {AUDIENCE_RU.get(r['audience'], r['audience'])}"
            f" · острота {r['intensity']} из 5\n"
            f"{esc(r['summary'])}\n"
            + fmt_quote(r)
            + "\n"
        )
    return "\n".join(lines)


def fmt_digest(stats: dict[str, Any], new_signals: int, new_msgs: int, top: list[Any]) -> str:
    lines = [
        "<b>Дайджест</b>",
        "",
        f"Новых сообщений: <b>{new_msgs}</b>",
        f"Новых сигналов: <b>{new_signals}</b>",
    ]
    ext = stats.get("extract") or {}
    if ext.get("pending_left"):
        lines.append(
            f"Ещё ждут разбора: <b>{discussions(ext['pending_left'])}</b> — "
            "разобрать их — кнопка 🧠 Разобрать"
        )
    if top:
        lines.append("\n<b>Топ болей сейчас</b>")
        for c in top:
            lines.append(
                f"• {esc(c['label'])} — {people_word(c['n_authors'])} → /pain_{c['id']}"
            )
        lines.append("\nПодробности — нажми /pain_… · весь отчёт: /report")
    return "\n".join(lines)


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_duration(seconds: float) -> str:
    """Человеческая длительность: «меньше минуты», «~7 мин», «~1 ч 20 мин»."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"~{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    minutes = round(minutes / 10) * 10  # точнее десяти минут всё равно не угадать
    if minutes == 60:
        hours, minutes = hours + 1, 0
    return f"~{hours} ч" + (f" {minutes} мин" if minutes else "")


def took(seconds: float) -> str:
    """«быстрее чем за минуту» / «за ~7 мин»."""
    d = fmt_duration(seconds)
    return "быстрее чем за минуту" if d == "меньше минуты" else f"за {d}"


def fmt_estimate(pending: int, seconds_per_thread: float | None) -> str:
    if not pending:
        return (
            "Разбирать нечего: все обсуждения уже разобраны.\n"
            "Новые появятся, когда пришлёшь свежий экспорт чата."
        )
    text = f"Ждут разбора: <b>{discussions(pending)}</b>."
    if seconds_per_thread is not None:
        text += f"\nВсе сразу — это {fmt_duration(seconds_per_thread * pending)}."
    else:
        text += "\nСколько это займёт, скажу после первого разбора — начни с 20."
    return text


def extract_choices(pending: int) -> list[tuple[str, str]]:
    """Кнопки выбора объёма: (подпись, значение для callback)."""
    options = [(discussions(n), str(n)) for n in (20, 100) if pending > n]
    if pending:
        options.append((f"Все {fmt_num(pending)}", "all"))
    return options


def fmt_extract_progress(done: int, total: int, stats: dict[str, Any]) -> str:
    return (
        f"🧠 Разбираю обсуждения: <b>{done} из {total}</b>\n"
        f"Найдено сигналов: {stats['signals']}\n\n"
        "<i>Можно остановить — уже разобранное сохранится.</i>"
    )


def fmt_extract_result(stats: dict[str, Any]) -> str:
    """Итог разбора для пользователя — без токенов и технических метрик."""
    lines = [
        f"✅ <b>Разбор закончен</b> {took(stats.get('seconds', 0))}",
        "",
        f"Разобрано: {discussions(stats['threads'] + stats['failed'])}",
        f"Найдено сигналов: <b>{stats['signals']}</b>",
    ]
    if stats["empty"]:
        lines.append(f"Без сигналов (болтовня, флуд): {stats['empty']}")
    if stats["failed"]:
        lines.append(f"Не получилось разобрать: {stats['failed']} — повторить: /retry")
    left = stats.get("pending_left", 0)
    lines.append(f"Ещё ждут разбора: {discussions(left)}" if left else "Разобрано всё.")
    return "\n".join(lines)


def fmt_stopped(what: str, job: dict[str, Any] | None = None) -> str:
    text = f"⏹ <b>Остановлено</b>: {esc(what)}."
    if job and what == "разбор" and job.get("done"):
        text += (
            f"\nУспели разобрать {discussions(job['done'])}, "
            f"найдено сигналов: {job.get('signals', 0)} — это сохранено."
            "\nОстальное ждёт в очереди."
        )
    elif what == "пересчёт болей":
        text += "\nБоли, которые успели пересчитать, сохранены. Можно запустить заново."
    return text


def fmt_busy(what: str, progress: str = "") -> str:
    tail = f" ({esc(progress)})" if progress else ""
    return (
        f"⏳ Сейчас идёт {esc(what)}{tail}.\n"
        "Дождись окончания или останови её кнопкой ⏹ под сообщением с прогрессом."
    )


def fmt_usage(report: dict[str, Any], price_in: float = 0.0, price_out: float = 0.0) -> str:
    """Скрытая статистика расхода модели (/usage)."""

    def cost(prompt: int, completion: int) -> str:
        if not (price_in or price_out):
            return ""
        value = prompt / 1e6 * price_in + completion / 1e6 * price_out
        return f" · ≈ {value:,.2f}".replace(",", " ")

    lines = ["<b>📈 Расход модели</b>"]
    if report["models"]:
        lines.append(f"<i>{esc(', '.join(report['models']))}</i>")
    lines.append("")
    for title, t in report["totals"]:
        tokens = t["prompt"] + t["completion"]
        reasoning = f", из них рассуждения {fmt_num(t['reasoning'])}" if t["reasoning"] else ""
        lines.append(
            f"<b>{title}</b>: {fmt_num(tokens)} токенов "
            f"(запрос {fmt_num(t['prompt'])}, ответ {fmt_num(t['completion'])}{reasoning})"
            f"{cost(t['prompt'], t['completion'])}"
        )

    st = report["by_stage"]
    ex = st.get("extract")
    lines += ["", "<b>В среднем (за 30 дней)</b>"]
    if ex and ex["threads"]:
        sec = ex["seconds"] / ex["threads"]
        sec_text = f"{sec:.1f}" if sec < 10 else f"{sec:.0f}"
        lines.append(
            f"• на одно обсуждение: ~{fmt_num(ex['tokens'] // ex['threads'])} токенов, "
            f"{sec_text} с"
        )
    if ex and ex["runs"]:
        lines.append(f"• на один разбор: ~{fmt_num(ex['tokens'] // ex['runs'])} токенов")
    recount = [st[k] for k in ("cluster", "cards") if k in st]
    if recount:
        runs = max(r["runs"] for r in recount)
        lines.append(
            f"• на один пересчёт болей: ~{fmt_num(sum(r['tokens'] for r in recount) // runs)} токенов"
        )
    month = report["totals"][2][1]
    if report["active_days"]:
        per_day = (month["prompt"] + month["completion"]) // report["active_days"]
        days = report["active_days"]
        lines.append(f"• за день работы: ~{fmt_num(per_day)} токенов "
                     f"(дней с работой: {days})")
    if not ex and not recount:
        lines.append("пока нет данных — появятся после первого разбора")

    total = sum(r["tokens"] for r in st.values())
    if total:
        names = {"extract": "разбор", "cluster": "группировка", "cards": "карточки"}
        parts = [f"{names.get(k, k)} {v['tokens'] * 100 // total}%" for k, v in st.items()]
        lines += ["", "Доля этапов за 30 дней: " + ", ".join(parts)]
    if not (price_in or price_out):
        lines += ["", "<i>Чтобы видеть стоимость, задай в .env LLM_PRICE_IN и LLM_PRICE_OUT "
                  "— цену за 1 млн токенов запроса и ответа.</i>"]
    return "\n".join(lines)


def fmt_connect_result(res: dict[str, list]) -> tuple[str, Any]:
    """Итог подключения сохранённых чатов: (текст, кнопки)."""
    lines = []
    if res["connected"]:
        lines.append("🔌 Подключил сохранённые чаты:")
        lines += [f"• {esc(t)}" for t in res["connected"]]
    if res["waiting"]:
        lines.append("\n⏳ Ждут одобрения админа чата:")
        lines += [f"• {esc(x)}" for x in res["waiting"]]
    if res["failed"]:
        lines.append("\n❌ Не получилось:")
        lines += [f"• {esc(link)} — {esc(err)}" for link, err in res["failed"]]
    markup = None
    if res["connected"]:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📥 Загрузить историю", callback_data="hist:all")
        ]])
    return "\n".join(lines).strip(), markup


def fmt_person(c: dict[str, Any]) -> str:
    """Карточка участника."""
    label = c["author_label"]
    name = c["name"] or "участник " + label.removeprefix("u:")
    head = f"👤 <b>{esc(name)}</b>" + (f" (@{esc(c['username'])})" if c.get("username") else "")
    lines = [head, ""]
    lines.append(f"Компания: {esc(c['company']) if c.get('company') else '— не указано'}")
    lines.append(f"Роль: {esc(c['role']) if c.get('role') else '— не указано'}")
    if c.get("note"):
        lines.append(f"Заметка: {esc(c['note'])}")
    if c.get("company_hint") or c.get("role_hint"):
        hint = ", ".join(esc(x) for x in (c.get("role_hint"), c.get("company_hint")) if x)
        lines += ["", f"💡 <b>Подсказка по словам самого участника:</b> {hint}"]
        if c.get("hint_quote"):
            lines.append(f"<blockquote>{esc(c['hint_quote'])}</blockquote>")
        if c.get("hint_link"):
            href = html.escape(c["hint_link"], quote=True)
            lines.append(f'<a href="{href}">↗ сообщение в чате</a>')
    if c.get("chats"):
        lines += ["", "<b>Пишет в чатах:</b>"]
        lines += [f"• {esc(ch['title'])} — {messages_word(ch['n'])}" for ch in c["chats"]]
    lines.append(f"\nСигналов от участника: {c.get('signals', 0)}")
    if c.get("pains"):
        lines.append("<b>Боли участника:</b>")
        lines += [f"• {esc(pn['label'])} → /pain_{pn['id']}" for pn in c["pains"]]
    return "\n".join(lines)


def fmt_people(rows: list[dict[str, Any]], title: str) -> str:
    if not rows:
        return "Участников пока нет — сначала загрузи переписку чата."
    lines = [f"👥 <b>Участники</b> · {esc(title)}", ""]
    unknown = 0
    for r in rows:
        who = people.display(r["name"], r["author_label"], r["company"], r["role"])
        mark = " 💡" if r.get("has_hint") and not (r["company"] or r["role"]) else ""
        if not (r["company"] or r["role"]):
            unknown += 1
        lines.append(f"• {esc(who)} — {messages_word(r['msgs'])}{mark} "
                     f"{people.who_command(r['author_label'])}")
    lines.append("")
    if unknown:
        lines.append(f"Компания и роль не указаны у {unknown} из {len(rows)}. "
                     "Нажми /who_… рядом с человеком, чтобы внести.")
    lines.append("💡 — нейросеть заметила, что человек сам сказал о себе; "
                 "подтвердить можно в карточке участника.")
    return "\n".join(lines)
