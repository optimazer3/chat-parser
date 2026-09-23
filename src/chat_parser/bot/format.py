"""Рендер сообщений бота. Telegram режет сообщения на 4096 символах."""

from __future__ import annotations

import html
import json
from typing import Any

LIMIT = 3800

AUDIENCE_RU = {
    "owner": "владельцы оптик",
    "staff": "персонал салонов",
    "optometrist": "оптометристы",
    "supplier": "поставщики",
    "customer": "покупатели",
    "unknown": "не определено",
}


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
        when = f"обновлён {c['last_run']:%d.%m %H:%M}" if c["last_run"] else "ещё не загружен"
        lines.append(f"{flag} <b>{esc(c['title'])}</b>")
        lines.append(f"   {c['msgs']} сообщ. · {when}")
        if c["retry_after"]:
            lines.append(f"   ⛔ флуд-пауза до {c['retry_after']:%d.%m %H:%M}")
    lines += ["", f"Ждут разбора: <b>{discussions(pending)}</b>"]
    if last_run:
        lines.append(f"Последний полный прогон: {last_run:%d.%m %H:%M} UTC")
    return "\n".join(lines)


def people(n: int) -> str:
    return f"{n} {plural(n, 'человек', 'человека', 'человек')}"


def chats_word(n: int) -> str:
    return f"{n} {plural(n, 'чате', 'чатах', 'чатах')}"


def signals_word(n: int) -> str:
    return f"{n} {plural(n, 'сигнал', 'сигнала', 'сигналов')}"


def fmt_top(clusters: list[Any]) -> str:
    if not clusters:
        return (
            "Болей пока нет. Сначала разбери обсуждения — 🧠 Разобрать, "
            "потом собери их — 🧩 Пересчитать боли."
        )
    lines = ["🔝 <b>Топ болей</b>"]
    current = None
    for c in clusters:
        if c["audience"] != current:
            current = c["audience"]
            lines.append(f"\n<b>{AUDIENCE_RU.get(current, current).capitalize()}</b>")
        lines.append(
            f"• {esc(c['label'])}\n"
            f"   говорят {people(c['n_authors'])} в {chats_word(c['n_chats'])}, "
            f"{signals_word(c['n_signals'])} → /pain_{c['id']}"
        )
    lines.append("\nНажми на /pain_… рядом с болью — пришлю подробности и цитаты.")
    return "\n".join(lines)


def fmt_card(cluster: Any, quotes: list[Any]) -> str:
    lines = [
        f"<b>#{cluster['id']} {esc(cluster['label'])}</b>",
        f"<i>{AUDIENCE_RU.get(cluster['audience'], cluster['audience'])}</i>",
        "",
        f"<blockquote>{esc(cluster['statement'])}</blockquote>",
        "",
        f"вес {cluster['score']:.1f} · {cluster['n_authors']} чел. · "
        f"{cluster['n_chats']} чат. · {cluster['n_signals']} сигн.",
    ]
    card = cluster["card"]
    if card:
        card = json.loads(card) if isinstance(card, str) else card
        lines += ["", f"<b>Кто:</b> {esc(card['who'])}", f"<b>Когда:</b> {esc(card['when'])}"]
        if card.get("current_workarounds"):
            lines.append("\n<b>Как выкручиваются:</b>")
            lines += [f"• {esc(w)}" for w in card["current_workarounds"]]
        if card.get("evidence"):
            lines.append("\n<b>Цитаты:</b>")
            lines += [f"<blockquote>{esc(q)}</blockquote>" for q in card["evidence"]]
        if card.get("product_hypotheses"):
            lines.append("\n<b>Гипотезы:</b>")
            lines += [f"• {esc(h)}" for h in card["product_hypotheses"]]
        if card.get("open_questions"):
            lines.append("\n<b>Выяснить интервью:</b>")
            lines += [f"• {esc(q)}" for q in card["open_questions"]]
    elif quotes:
        lines.append("\n<b>Цитаты:</b>")
        lines += [f"<blockquote>{esc(q['evidence_quote'])}</blockquote>" for q in quotes]
    return "\n".join(lines)


def fmt_signals(rows: list[Any]) -> str:
    if not rows:
        return "Сигналов пока нет."
    lines = ["<b>Последние сигналы</b>", ""]
    for r in rows:
        lines.append(
            f"<b>{esc(r['type'])}</b> · {AUDIENCE_RU.get(r['audience'], r['audience'])}"
            f" · острота {r['intensity']}\n"
            f"{esc(r['summary'])}\n"
            f"<blockquote>{esc(r['evidence_quote'])}</blockquote>"
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
            "доразберутся в следующие ночи или сразу по кнопке 🧠 Разобрать"
        )
    if top:
        lines.append("\n<b>Топ болей сейчас</b>")
        for c in top:
            lines.append(
                f"• {esc(c['label'])} — {people(c['n_authors'])} → /pain_{c['id']}"
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
