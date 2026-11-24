"""Сквозной тест бота: настоящий Dispatcher и обработчики, живой Postgres,
фейковый транспорт Telegram и подставная модель.

Нужна отдельная тестовая база (таблицы в ней пересоздаются!):
    TEST_DATABASE_URL=postgresql://user@localhost:5432/chatparser_test pytest
Без переменной тест пропускается.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL не задан")

ADMIN = 777
LINE = re.compile(r"^\[m:(\d+) \| (u:\w+) \| [^\]]*\] (.+)$")


def _export_bytes() -> bytes:
    t0 = int(datetime(2026, 2, 3, 11, 0, tzinfo=timezone.utc).timestamp())

    def m(i, mins, uid, text, reply=None):
        d = {"id": i, "type": "message", "date_unixtime": str(t0 + mins * 60),
             "from_id": f"user{uid}", "text": text}
        if reply:
            d["reply_to_message_id"] = reply
        return d

    msgs = [
        {"id": 1, "type": "service", "date_unixtime": str(t0)},
        m(2, 1, 111, "коллеги, чем меряете межзрачковое кроме линейки?"),
        m(3, 3, 222, "пупиллометром, но он у нас один на два салона, возим туда-сюда", 2),
        m(4, 4, 222, "второй брать за 40к жаба душит при нашем потоке"),
        m(6, 40, 444, "а у кого какие сроки по оправам от поставщика? жду по 6 недель"),
        m(7, 42, 111, "та же беда, клиенты уходят к сетевым, а мы ждём", 6),
        m(9, 200, 555, "подскажите чем полировать полимерные линзы, паста царапает покрытие"),
    ]
    data = {"name": "Оптики — обмен опытом", "type": "public_supergroup",
            "id": 1234567890, "messages": msgs}
    return json.dumps(data, ensure_ascii=False).encode()


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, File, Message, User

    from chat_parser import db
    from chat_parser.analyze.schema import (
        AssignItem, Assignment, Card, ClusterDraft, Clustering, DayDigest, Extraction,
        Merging, PersonAbout, PersonHint, PostBatch, PostIdea, PostIdeas, PostTheme, PostThemes, PostVerdict, Signal,
        WeeklyDigest,
    )
    from chat_parser.llm import LLMError
    from chat_parser.people import parse_fields
    from chat_parser.bot import jobs
    from chat_parser.bot.auth import AdminOnly
    from chat_parser.bot.handlers import router
    from chat_parser.config import settings
    from chat_parser.llm import LLM

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "database_url", TEST_DB)
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "fake")
    monkeypatch.setattr(settings, "tg_api_id", None)
    monkeypatch.setattr(settings, "tg_api_hash", "")
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(settings, "tg_admin_ids", str(ADMIN))
    state = {"cluster_label": "Нехватка оборудования"}

    async def fake_structured(self, system, user, schema, max_tokens=8000):
        state.setdefault("prompts", []).append(user)
        self.usage["calls"] += 1
        self.usage["prompt"] += 900
        self.usage["completion"] += 600
        if schema is Extraction:
            lines = [m for m in map(LINE.match, user.splitlines()) if m]
            sigs = [
                Signal(type="pain", audience="owner", summary=m.group(3)[:100],
                       evidence_quote=" ".join(m.group(3).split()[:5]),
                       message_ids=[int(m.group(1))], author_label=m.group(2),
                       intensity=5 if "срочно" in m.group(3) else 3,
                       confidence=0.9, entities=[], context="тест")
                for m in lines if len(m.group(3).split()) >= 5
            ]
            # человек сам рассказал о себе — и ещё кто-то рассказал о нём (не считается)
            hints = [
                PersonHint(author_label=m.group(2), company="Оптика Люкс", role="владелец",
                           evidence_quote="у меня два салона Оптика Люкс")
                for m in lines if "два салона Оптика Люкс" in m.group(3)
            ]
            return Extraction(has_signals=bool(sigs), signals=sigs, people=hints)
        if schema is Clustering:
            ids = [int(x) for x in re.findall(r"^(\d+)\t", user, re.M)]
            return Clustering(clusters=[ClusterDraft(
                label=state["cluster_label"], statement="мне не хватает приборов",
                signal_ids=ids)])
        if schema is Assignment:
            known = user.split("Известные боли", 1)[1].split("Новые сигналы", 1)
            first_pain = int(re.search(r"^(\d+)\t", known[0].split("\n", 1)[1], re.M)[1])
            return Assignment(items=[
                AssignItem(signal_id=int(sid),
                           pain_id=first_pain if re.search("прибор|пупиллометр", text) else 0)
                for sid, text in re.findall(r"^(\d+)\t\w+\t(.*)$", known[1], re.M)
            ])
        if schema is DayDigest:
            return DayDigest(summary="Сутки прошли под знаком поставок и нехватки приборов.",
                             opportunities=["Региональный склад оправ с доставкой за 3 дня"],
                             highlights=["Поставщики срывают сроки, владельцы теряют клиентов",
                                         "Не хватает диагностических приборов"])
        if schema is Merging:
            return Merging(groups=[])
        if schema is Card:
            ids = [int(x) for x in re.findall(r"\[id (\d+)\]", user)][:3]
            return Card(title="t", statement="s", who="владельцы", when="при открытии",
                        current_workarounds=["возят прибор"], evidence_ids=ids,
                        product_hypotheses=["аренда"], open_questions=["сколько салонов"])
        if schema is PostIdeas:
            publications = json.loads(user)
            return PostIdeas(items=[PostIdea(
                title="Как выбрать оправы к сезону", angle="Показать свой чек-лист отбора оправ.",
                evidence_ids=[publications[0]["id"]],
            )])
        if schema is PostThemes:
            posts = json.loads(user)["публикации"]
            return PostThemes(items=[
                PostTheme(id=int(mid), theme=(
                    "Мотивация продавцов" if "бонус" in text or "преми" in text
                    else "Закупка оправ"
                ))
                for mid, text in posts.items()
            ])
        if schema is PostBatch:
            items = []
            for block in user.split("Сообщения:\n", 1)[1].split("\n\n[id "):
                head, body = block.lstrip("[id ").split("\n", 1)
                low = body.lower()
                is_post = len(body) >= 200 and not low.startswith("кто знает")
                theme = ("Мотивация продавцов" if "мотивац" in low else
                         "Закупка оправ" if "оправ" in low else
                         "Аренда помещения" if "аренд" in low else "Разное")
                items.append(PostVerdict(id=int(head.split("]")[0].split()[0]), is_post=is_post,
                                         theme=theme if is_post else "",
                                         summary=body[:60] if is_post else ""))
            return PostBatch(items=items)
        if schema is WeeklyDigest:
            return WeeklyDigest(
                summary="Лучше всего заходят посты про мотивацию продавцов.",
                focus=["Мотивация продавцов откликается сильнее всего."],
                others=["У других выделяется аренда помещения."],
                ideas=["Разбор системы бонусов для продавцов."])
        if schema is PersonAbout:
            if state.get("person_fail"):
                raise LLMError("модель не ответила")
            company, role, details = parse_fields(user)
            return PersonAbout(company=company or "", role=role or "", details=details or "")
        raise AssertionError(schema)

    monkeypatch.setattr(LLM, "structured", fake_structured)
    export = _export_bytes()

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.mid = 1000
            self.sent: list[str] = []
            self.log: list[str] = []
            self.labels: list[str] = []  # подписи кнопок последнего ответа
            self.files: list[tuple] = []  # (имя, байты, подпись) отправленных файлов
            self.documents = 0

        async def close(self):
            pass

        async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                                 raise_for_status=True):
            yield export

        def _msg(self, bot, mid, text):
            return Message(message_id=mid, date=datetime.now(timezone.utc),
                           chat=Chat(id=ADMIN, type="private"), text=text).as_(bot)

        async def make_request(self, bot, method, timeout=None):
            name = type(method).__name__
            if name == "GetMe":
                return User(id=1, is_bot=True, first_name="bot", username="test_bot")
            if name == "AnswerCallbackQuery":
                return True
            if name == "GetFile":
                return File(file_id="f", file_unique_id="u", file_path="d/result.json")
            if name in ("SendMessage", "EditMessageText"):
                text = method.text
                rm = getattr(method, "reply_markup", None)
                # ForceReply и ReplyKeyboardRemove в Telegram прячут кнопки внизу
                assert type(rm).__name__ not in ("ForceReply", "ReplyKeyboardRemove"), text
                if rm is not None and hasattr(rm, "inline_keyboard"):
                    self.labels += [b.text for row in rm.inline_keyboard for b in row]
                    text += " " + " ".join(
                        f"[{b.callback_data}]" for row in rm.inline_keyboard for b in row
                    )
                self.sent.append(text)
                self.log.append(text)
                if name == "SendMessage":
                    self.mid += 1
                    return self._msg(bot, self.mid, method.text)
                return self._msg(bot, method.message_id, method.text)
            if name == "DeleteMessage":
                return True
            if name == "EditMessageReplyMarkup":
                return self._msg(bot, method.message_id, None)
            if name == "SendDocument":
                self.documents += 1
                self.mid += 1
                doc = method.document
                data = getattr(doc, "data", None)
                self.files.append((getattr(doc, "filename", None), data, method.caption))
                self.sent.append(method.caption or "")
                self.log.append(method.caption or "")
                return self._msg(bot, self.mid, None)
            raise AssertionError(f"неожиданный метод {name}")

    session = FakeSession()
    bot = Bot(token="42:TEST", session=session,
              default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    guard = AdminOnly({ADMIN})
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(router)

    from chat_parser.bot import handlers
    handlers._awaiting.clear()

    pool = await db.get_pool()
    await pool.execute(
        "drop table if exists runs, clusters, signals, threads, cursors, messages, chats, "
        "llm_usage, chat_requests, bot_settings, authors cascade"
    )
    await db.apply_schema()

    class H:
        pass

    h = H()
    h.session, h.pool, h.jobs, h.bot, h.state = session, pool, jobs, bot, state
    h.counter = 0

    async def feed(update_kwargs):
        from aiogram.types import Update
        h.counter += 1
        session.sent.clear()
        session.labels.clear()
        await dp.feed_update(bot, Update(update_id=h.counter, **update_kwargs))
        return "\n".join(session.sent)

    def msg(text=None, frm=ADMIN, **kw):
        user = User(id=frm, is_bot=False, first_name="u")
        return {"message": Message(
            message_id=h.counter + 1, date=datetime.now(timezone.utc),
            chat=Chat(id=ADMIN, type="private"), from_user=user, text=text, **kw)}

    async def say(text, frm=ADMIN, reply_to=None):
        kw = {}
        if reply_to is not None:
            kw["reply_to_message"] = Message(
                message_id=1, date=datetime.now(timezone.utc),
                chat=Chat(id=ADMIN, type="private"), text=reply_to)
        return await feed(msg(text, frm, **kw))

    async def press(data):
        from aiogram.types import CallbackQuery
        user = User(id=ADMIN, is_bot=False, first_name="u")
        m = Message(message_id=5000, date=datetime.now(timezone.utc),
                    chat=Chat(id=ADMIN, type="private"), text="меню")
        return await feed({"callback_query": CallbackQuery(
            id=str(h.counter), from_user=user, chat_instance="x", data=data, message=m)})

    async def send_export():
        from aiogram.types import Document
        return await feed(msg(document=Document(
            file_id="f", file_unique_id="u1", file_name="result.json", file_size=len(export))))

    h.say, h.press, h.send_export = say, press, send_export
    yield h
    await db.close_pool()
    # router — модульный синглтон, aiogram разрешает подключить его к одному
    # диспетчеру. В проде бот стартует раз за процесс, в тестах — много раз.
    router._parent_router = None


async def test_full_flow_through_bot(harness, monkeypatch):
    from chat_parser.config import settings

    h = harness
    everything = []  # всё, что бот сказал в обычном сценарии

    async def say(text, frm=ADMIN):
        out = await h.say(text, frm)
        everything.append(out)
        return out

    async def press(data):
        out = await h.press(data)
        everything.append(out)
        return out

    assert await say("/status", frm=999) == ""  # чужих молча игнорируем

    out = await h.send_export()
    everything.append(out)
    assert "Обсуждений в чате: 3" in out
    assert "Ждут разбора: <b>3 обсуждения</b>" in out
    assert "[ex:all]" in out and "[cancel]" in out  # меню можно закрыть
    assert "после первого разбора" in out

    out = await press("ex:all")
    n_signals = await h.pool.fetchval("select count(*) from signals")
    assert n_signals > 0
    assert f"Найдено сигналов: <b>{n_signals}</b>" in out
    assert "[stop:" in out  # пока шёл разбор, под прогрессом была кнопка ⏹
    assert "Разобрано всё." in out and "[cl]" in out

    out = await press("cl")
    assert "Болей найдено: <b>1</b>" in out
    assert "Нехватка оборудования" in out

    out = await say("📊 Статус")
    assert f"Сигналов: <b>{n_signals}</b>" in out
    assert "ещё не загружен" not in out

    cid = await h.pool.fetchval("select id from clusters")
    top = await say("🔝 Топ болей")
    assert f"/pain_{cid}" in top and "говорят 4 человека в 1 чате" in top
    card = await say(f"/pain_{cid}")  # нажимаемая ссылка из топа
    assert "возят прибор" in card
    # под цитатами — ссылки на сообщения приватной супергруппы
    assert card.count('<a href="https://t.me/c/1234567890/') == 3
    assert "<b>Выяснить:</b>" in card and "интервью" not in card
    sig = await say("/signals")
    assert '<a href="https://t.me/c/1234567890/' in sig

    # сигнал «из старой версии»: модель поставила первым не то сообщение —
    # ссылка всё равно ведёт туда, где цитата стоит на самом деле
    await h.pool.execute(
        "update signals set message_ids = array[2, 3], "
        "evidence_quote = 'он у нас один на два салона' where id = "
        "(select min(id) from signals)"
    )
    sig = await say("/signals 30")
    assert '<a href="https://t.me/c/1234567890/3">' in sig
    assert "pain" not in sig and "<b>боль</b>" in sig
    assert "возят прибор" in await say(f"/pain {cid}")   # и набранная руками
    assert "номера меняются" in await say("/pain_999999")

    before = h.session.documents
    assert "PDF отправлен сюда" in await say("📄 Отчёт")
    assert h.session.documents == before + 1
    assert h.session.files[-1][1].startswith(b"%PDF")

    out = await say("/redo")
    assert "[redo:yes]" in out
    out = await press("redo:yes")
    assert "Вернул в очередь: 3 обсуждения" in out
    assert "Все сразу — это" in out  # после первого разбора есть оценка времени

    assert "Неудачных обсуждений нет" in await say("/retry")
    assert "Запомнил" in await say("/addchat @optika_pro")  # без ключей — в ожидание

    # пользователю токены не показываем нигде в обычном сценарии
    assert not any("токен" in text for text in everything)

    # а скрытая статистика их знает — и по разбору, и по пересчёту болей
    monkeypatch.setattr(settings, "llm_price_in", 0.8)
    monkeypatch.setattr(settings, "llm_price_out", 3.2)
    out = await h.say("/usage")
    assert "≈" in out  # со стоимостью: суммы из Postgres приходят Decimal
    assert "токенов" in out and "на одно обсуждение" in out and "на один пересчёт болей" in out
    stages = {r["stage"] for r in await h.pool.fetch("select stage from llm_usage")}
    assert stages == {"extract", "cluster", "cards"}

    # второй тяжёлый процесс не стартует, пока идёт первый
    async with h.jobs.exclusive("разбор"):
        assert "Сейчас идёт разбор" in await h.press("ex:all")

    # большая очередь -> /run сначала спрашивает
    monkeypatch.setattr(settings, "bot_confirm_threshold", 1)
    out = await h.say("/run")
    assert "[run:all]" in out and "[run:20]" in out and "[cancel]" in out
    assert await h.pool.fetchval("select count(*) from threads where status='pending'") == 3


async def test_stop_button_during_extract(harness, monkeypatch):
    """Нажали «Разобрать» случайно — останавливаем, уже сделанное остаётся."""
    import asyncio

    from chat_parser.llm import LLM

    h = harness
    await h.send_export()

    real = LLM.structured
    gate = asyncio.Event()

    async def slow(self, *a, **kw):
        await gate.wait()  # модель «думает», пока мы не нажмём стоп
        return await real(self, *a, **kw)

    monkeypatch.setattr(LLM, "structured", slow)
    run = asyncio.create_task(h.press("ex:all"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if h.jobs.is_running():
            break
    job = h.jobs.current()
    assert job is not None

    out_stop = await h.press(f"stop:{job['id']}")
    await asyncio.wait_for(run, 5)
    assert not h.jobs.is_running()
    assert "Остановлено" in "\n".join(h.session.log)
    # ничего не разобрано — все обсуждения по-прежнему ждут
    assert await h.pool.fetchval("select count(*) from threads where status='pending'") == 3
    assert await h.pool.fetchval("select count(*) from signals") == 0
    # старая кнопка ⏹ после завершения ничего не ломает
    await h.press(f"stop:{job['id']}")
    assert out_stop == "" or "Останавливаю" not in out_stop


async def test_add_chat_without_api_keys_is_remembered(harness):
    h = harness
    out = await h.say("➕ Добавить чат")
    assert "Чаты, за которыми я слежу</b> · 0" in out and "пришли ссылку на чат" in out

    out = await h.say("https://t.me/Optika_Pro/")
    assert "Запомнил: https://t.me/optika_pro" in out and "ключи Telegram API" in out
    assert "уже в списке ожидания" in await h.say("@optika_pro")  # тот же чат иначе записан
    assert "приватном чате" in await h.say("https://t.me/c/1234567890/55")

    out = await h.say("/chats")
    assert "Ждут подключения" in out and "https://t.me/optika_pro" in out
    assert await h.pool.fetchval("select status from chat_requests") == "pending"


@pytest.fixture
def telegram_api(monkeypatch):
    """Ключи «есть»: подменяем подключение и Telegram-часть сборщика."""
    from chat_parser.bot import jobs
    from chat_parser.config import settings
    from chat_parser.ingest import collector

    monkeypatch.setattr(settings, "tg_api_id", 20481234)
    monkeypatch.setattr(settings, "tg_api_hash", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    monkeypatch.setattr(settings, "join_pause", 0)

    from telethon.tl.types import Channel, ChatPhotoEmpty
    from telethon.tl.types import User as TgUser

    class FakeClient:
        async def disconnect(self):
            pass

        async def get_entity(self, ref):
            known = {
                "petr_lens": TgUser(id=555, first_name="Пётр", last_name="Смирнов",
                                    username="petr_lens"),
                "optika_news": Channel(id=9, title="Новости оптики", photo=ChatPhotoEmpty(),
                                       date=None, username="optika_news"),
            }
            if ref not in known:
                raise ValueError(f'No user has "{ref}" as username')
            return known[ref]

    async def connect_client():
        return FakeClient()

    joined: list[str] = []

    async def register_chat(client, pool, ref, join=False):
        if "+approval" in ref:
            raise collector.JoinPending("заявка отправлена")
        if "+" in ref and not join:
            raise collector.NeedsJoin("«Закрытые оптики»: приватный чат, нужно вступить")
        if join:
            joined.append(ref)
        chat_id = -1009000000000 - len(ref)
        username = None if "+" in ref else ref.rsplit("/", 1)[-1]
        await pool.execute(
            "insert into chats (id, title, username, link) values ($1,$2,$3,$4) "
            "on conflict (id) do nothing",
            chat_id, "Оптики Про" if username else "Закрытые оптики", username, ref,
        )
        await pool.execute("insert into cursors (chat_id) values ($1) on conflict do nothing",
                           chat_id)
        return chat_id

    async def sync_chat_full(client, pool, chat_id, on_progress=None):
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        for i, text in enumerate(
            ["подскажите поставщика линз, нынешний задерживает на месяц",
             "у нас так же, клиенты уходят к сетевым", "берём у двух сразу"], start=1):
            await pool.execute(
                "insert into messages (chat_id, message_id, ts, author_label, text) "
                "values ($1,$2,$3,$4,$5) on conflict do nothing",
                chat_id, i, t0 + timedelta(minutes=i), f"u:{i}", text,
            )
        await pool.execute("update cursors set backfill_done = true, newest_id = 3, "
                           "last_run = now() where chat_id = $1", chat_id)
        if on_progress:
            await on_progress(3)
        return {"chat_id": chat_id, "saved": 3}

    monkeypatch.setattr(jobs, "connect_client", connect_client)
    monkeypatch.setattr(collector, "register_chat", register_chat)
    monkeypatch.setattr(collector, "sync_chat_full", sync_chat_full)
    return joined


async def test_add_public_chat_loads_history(harness, telegram_api):
    h = harness
    out = await h.say("t.me/optika_pro")
    assert "Подключил чат <b>Оптики Про</b>" in out
    assert "Загружено: <b>3 сообщения</b>" in out
    assert "[stop:" in out  # во время загрузки была кнопка ⏹
    assert "[ex:all]" in out  # сразу предлагает разобрать

    chats = await h.say("/chats")
    assert '<a href="https://t.me/optika_pro">Оптики Про</a>' in chats
    assert "3 сообщения, за сутки 0 · 3 человека · 0 сигналов" in chats and "сообщ." not in chats
    assert telegram_api == []  # в открытый чат вступать не пришлось


async def test_private_invite_asks_before_joining(harness, telegram_api):
    h = harness
    out = await h.say("https://t.me/+Wfx5U9BAtSRiODVi")
    assert "нужно вступить" in out and "[join:" in out and "[drop:" in out
    assert telegram_api == []  # без подтверждения не вступаем

    token = out.split("[join:")[1].split("]")[0]
    out = await h.press(f"join:{token}")
    assert "Подключил чат <b>Закрытые оптики</b>" in out
    assert telegram_api == ["https://t.me/+Wfx5U9BAtSRiODVi"]


async def test_join_request_waits_for_admin(harness, telegram_api):
    h = harness
    out = await h.say("https://t.me/+approvalAbcdef")
    assert "заявку на вступление я отправил" in out
    assert await h.pool.fetchval("select status from chat_requests") == "join_pending"


async def test_saved_chats_connect_when_keys_appear(harness, telegram_api):
    """Ссылки, сохранённые без ключей, подключаются сами, когда ключи есть."""
    from chat_parser.bot import jobs

    h = harness
    await h.pool.execute("insert into chat_requests (link) values "
                         "('https://t.me/optika_pro'), ('https://t.me/+approvalXyzxyz')")
    res = await jobs.connect_saved_chats()
    assert res["connected"] == ["Оптики Про"]
    assert res["waiting"] == ["https://t.me/+approvalXyzxyz"]
    assert await jobs.chats_without_history() == [
        await h.pool.fetchval("select id from chats where username = 'optika_pro'")
    ]


async def test_connect_saved_chats_by_button(harness, telegram_api):
    """Без ночного прогона ожидающие чаты подключаются кнопкой в /chats."""
    h = harness
    await h.pool.execute("insert into chat_requests (link) values ('https://t.me/optika_pro')")
    out = await h.say("/chats")
    assert "подключу по кнопке ниже" in out and "[connect:saved]" in out

    out = await h.press("connect:saved")
    assert "Подключил сохранённые чаты" in out and "Оптики Про" in out
    assert "[hist:all]" in out

    out = await h.press("hist:all")
    assert "Загружено: <b>3 сообщения</b>" in out and "[ex:all]" in out
    assert "Ждут подключения" not in await h.say("/chats")


# ------------------------------------------------------------ вечерний отчёт

PRO = -1009000000001  # «Оптики Про»: подключён, история не загружалась
AUTHORS = {
    "a": ("a" * 16, "u:aaaaaaaa", "Иван Петров", "ivan_optika"),
    "b": ("b" * 16, "u:bbbbbbbb", "Мария", None),
    "c": ("c" * 16, "u:cccccccc", "Ольга Смирнова", "olga"),
}
TODAY = [  # (автор, текст, ответ на)
    ("a", "у меня два салона Оптика Люкс в Казани, поставщик оправ опять сорвал сроки", None),
    ("b", "у нас тоже поставщик линз задерживает поставки уже третью неделю", 1),
    ("c", "срочно нужен второй пупиллометр, один прибор на два салона не спасает", None),
    ("b", "авторефрактометр сломался, приборов не хватает, клиентов некуда посадить", None),
    ("a", "ещё прибор для проверки линз нужен, старый еле работает совсем", None),
]


@pytest.fixture
async def evening(harness, telegram_api, monkeypatch):
    """Аккаунт-сборщик «читает» чаты: сегодня в «Оптиках Про» пять сообщений.
    Почта настроена, письма перехватываются в h.letters."""
    from chat_parser import mailer
    from chat_parser.bot import live, weekly
    from chat_parser.config import settings
    from chat_parser.ingest import collector
    from chat_parser.people import upsert_authors

    h = harness
    monkeypatch.setattr(settings, "live_quiet_minutes", 0)
    monkeypatch.setattr(settings, "report_email_to", "owner@example.com")
    monkeypatch.setattr(settings, "smtp_user", "bot@gmail.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    h.letters = []
    monkeypatch.setattr(mailer, "_send_sync", h.letters.append)
    monkeypatch.setattr(live, "connect_client", telegram_api_client)
    monkeypatch.setattr(weekly, "connect_client", telegram_api_client)
    await h.pool.execute("insert into chats (id, title, username) values ($1, $2, $3)",
                         PRO, "Оптики Про", "optika_pro")
    await h.pool.execute("insert into cursors (chat_id) values ($1)", PRO)

    h.calls, h.inbox = [], {PRO: list(TODAY)}

    async def sync_chat(client, pool, chat_id, mode, limit=None, on_progress=None, since=None):
        h.calls.append((chat_id, mode, since))
        todo, h.inbox[chat_id] = h.inbox.get(chat_id, []), []
        if not todo:
            return {"chat_id": chat_id, "saved": 0}
        first = await pool.fetchval(
            "select coalesce(max(message_id), 0) + 1 from messages where chat_id = $1", chat_id)
        t0 = datetime.now(timezone.utc) - timedelta(minutes=len(todo) + 2)
        async with pool.acquire() as conn:
            for i, (who, text, reply) in enumerate(todo):
                ah, label, name, username = AUTHORS[who]
                await conn.execute(
                    "insert into messages (chat_id, message_id, ts, author_hash, author_label, "
                    "text, reply_to) values ($1,$2,$3,$4,$5,$6,$7)",
                    chat_id, first + i, t0 + timedelta(minutes=i), ah, label, text, reply)
            await upsert_authors(conn, [AUTHORS[w] for w, _, _ in todo])
        await pool.execute("update cursors set newest_id = $2, last_run = now() "
                           "where chat_id = $1", chat_id, first + len(todo) - 1)
        return {"chat_id": chat_id, "saved": len(todo)}

    monkeypatch.setattr(collector, "sync_chat", sync_chat)
    return h


def letter_pdf(letter) -> bytes:
    (att,) = list(letter.iter_attachments())
    return att.get_content()


def pdf_text(pdf: bytes) -> str:
    import io

    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def pdf_links(pdf: bytes) -> list[str]:
    import io

    from pypdf import PdfReader

    return [a.get_object().get("/A", {}).get("/URI")
            for p in PdfReader(io.BytesIO(pdf)).pages for a in p.get("/Annots", [])]


async def telegram_api_client():
    class FakeClient:
        async def disconnect(self):
            pass

    return FakeClient()


async def _old_pain(h) -> int:
    """Архив из файла разобран вручную: есть известная боль «Нехватка оборудования»."""
    await h.send_export()
    await h.press("ex:all")
    await h.press("cl")
    h.state["cluster_label"] = "Срыв сроков поставки"
    return await h.pool.fetchval("select id from clusters")


async def test_evening_paused_and_budget(evening, monkeypatch):
    from chat_parser.bot import live
    from chat_parser.config import settings

    h = evening
    await h.send_export()  # архив не разбирали — вечером он не разбирается сам

    await live.set_enabled(False)
    text = (await live.build_report()).text
    assert "⏸ Разбор на паузе" in text and "Оптики Про</a>: 5 сообщений" in text
    assert await h.pool.fetchval("select count(*) from signals") == 0
    assert await h.pool.fetchval("select count(*) from llm_usage where source = 'live'") == 0
    assert "В архиве ждут ручного разбора: 3 обсуждения" in text

    await live.set_enabled(True)
    monkeypatch.setattr(settings, "llm_price_in", 10.0)
    monkeypatch.setattr(settings, "llm_price_out", 10.0)
    await h.pool.execute("insert into llm_usage (stage, prompt_tokens, source) "
                         "values ('extract', 5000000, 'manual')")
    assert await live.budget_left() == pytest.approx(30.0)  # ручной разбор не в счёт
    await h.pool.execute("insert into llm_usage (stage, prompt_tokens, source) "
                         "values ('extract', 3100000, 'live')")
    h.inbox[PRO] = [("c", "срочно ищем мастера по ремонту оправ, никто не берётся", None)]
    text = (await live.build_report()).text
    assert "⚠️ Дневной лимит на автоматический разбор исчерпан в" in text
    assert await h.pool.fetchval("select count(*) from signals") == 0
    assert await h.pool.fetchval(
        "select count(*) from threads where chat_id = $1 and status = 'pending'", PRO) >= 1
    assert await live.spent_today() == pytest.approx(31.0)


async def test_discussion_going_at_report_time_is_not_lost(evening, monkeypatch):
    """Обсуждение шло в момент отчёта — его разберут следующим вечером, хотя
    закончилось оно раньше начала следующего периода."""
    from chat_parser.bot import live
    from chat_parser.config import settings

    h = evening
    monkeypatch.setattr(settings, "live_quiet_minutes", 60)  # люди ещё пишут
    await live.build_report()
    assert await h.pool.fetchval("select count(*) from signals") == 0

    monkeypatch.setattr(settings, "live_quiet_minutes", 0)
    text = (await live.build_report()).text
    assert await h.pool.fetchval("select count(*) from signals") == 5
    assert "Всего за день: 5 сигналов" in text


async def test_people_and_companies(evening):
    from chat_parser.bot import live

    h = evening
    await _old_pain(h)
    await live.build_report(mark_sent=False)  # «Итоги дня сейчас» — вечерний всё равно придёт

    card = await h.say("/who_aaaaaaaa")
    assert "👤 <b>Иван Петров</b> (@ivan_optika)" in card
    assert "Компания: — не указано" in card
    assert "по словам самого участника:</b> владелец, Оптика Люкс" in card
    assert "у меня два салона Оптика Люкс" in card and "https://t.me/optika_pro/1" in card
    assert "Оптики Про — 2 сообщения" in card and "Срыв сроков поставки" in card
    assert "[pa:aaaaaaaa]" in card and "[pe:aaaaaaaa]" in card

    out = await h.press("pa:aaaaaaaa")
    assert "✅ Сохранил." in out and "Компания: Оптика Люкс" in out and "Роль: владелец" in out
    assert "[pa:" not in out

    # Марии нейросеть ничего не подсказала — вносим руками: кнопка, потом ответ
    assert "[pa:" not in await h.say("/who_bbbbbbbb")
    prompt = await h.press("pe:bbbbbbbb")
    assert "✏️ Компания и роль для u:bbbbbbbb (Мария)" in prompt and "[cancel]" in prompt
    out = await h.say("Линзы Плюс, продавец")
    assert "Компания: Линзы Плюс" in out and "Роль: продавец" in out
    await h.press("pn:bbbbbbbb")
    assert "Заметка: знакомы по выставке" in await h.say("знакомы по выставке")
    prompt = await h.press("pn:bbbbbbbb")
    assert "Заметка:" not in await h.say("-", reply_to=prompt)  # и через «Ответить»
    await h.press("pe:bbbbbbbb")
    out = await h.say("-")
    assert "Компания: — не указано" in out and "Роль: — не указано" in out
    assert "нет в базе" in await h.say("/who_deadbeef")

    ppl = await h.say("/people")
    assert "Иван Петров · Оптика Люкс, владелец — 2 сообщения /who_aaaaaaaa" in ppl
    assert "не указаны у 2 из 3" in ppl
    out = await h.press(f"ppl:{PRO}")
    assert "Участники</b> · Оптики Про" in out and f"[names:{PRO}]" in out

    # подпись под цитатой — везде, где есть цитаты
    assert "— Иван Петров · Оптика Люкс, владелец /who_aaaaaaaa" in await h.say("/signals 30")
    report = (await live.build_report(mark_sent=False)).text
    assert "Ольга Смирнова /who_cccccccc" in report

    # имена и ники в нейросеть не уходят
    prompts = "\n".join(h.state["prompts"])
    assert "Иван" not in prompts and "ivan_optika" not in prompts and "Ольга" not in prompts


async def test_delete_chat_from_list(evening):
    from chat_parser.bot import live

    h = evening
    cid = await _old_pain(h)
    await live.build_report()
    await h.pool.execute("insert into chat_requests (link) values ('https://t.me/optika_new')")

    chats = await h.say("➕ Добавить чат")
    assert '<a href="https://t.me/optika_pro">Оптики Про</a></b>' in chats
    assert "5 сообщений, за сутки 5 · 3 человека · 5 сигналов" in chats
    assert f"[del:{PRO}]" in chats and "[delreq:" in chats and "[ppl:" not in chats
    assert "🗑 Удалить: Оптики Про" in h.session.labels

    out = await h.press(f"del:{PRO}")
    assert "Удалить чат <b>Оптики Про</b>?" in out and f"[delok:{PRO}]" in out
    assert await h.pool.fetchval("select count(*) from chats where id = $1", PRO) == 1

    out = await h.press(f"delok:{PRO}")
    assert "✅ Чат <b>Оптики Про</b> удалён: 5 сообщений, 5 сигналов." in out
    assert "Болей, в которых не осталось сигналов: 1 — убрал." in out
    for table in ("chats", "cursors", "messages", "threads", "signals"):
        col = "id" if table == "chats" else "chat_id"
        assert await h.pool.fetchval(f"select count(*) from {table} where {col} = $1", PRO) == 0
    assert [r["id"] for r in await h.pool.fetch("select id from clusters")] == [cid]
    assert "уже нет в списке" in await h.press(f"delok:{PRO}")

    req = await h.pool.fetchval("select id from chat_requests")
    assert "Убрал из ожидания: https://t.me/optika_new" in await h.press(f"delreq:{req}")
    chats = await h.say("➕ Добавить чат")
    assert "Оптики Про" not in chats and "Ждут подключения" not in chats


async def test_new_keyboard_is_sent_once(harness):
    from chat_parser.bot import main

    h = harness
    h.session.sent.clear()
    await main.announce_keyboard(h.bot)
    await main.announce_keyboard(h.bot)
    assert len(h.session.sent) == 1


# ---------------------------------------------------------- время отчёта


async def test_schedule_edge_cases(harness, monkeypatch):
    from chat_parser import clock, db
    from chat_parser.bot import live

    t = {"now": clock.now().replace(hour=20, minute=0, second=0, microsecond=0)}
    day1 = t["now"]
    monkeypatch.setattr(clock, "now", lambda: t["now"])
    slot = day1.replace(hour=23, minute=30)
    await live.set_report_time(23, 30)

    # бот был выключен в 23:30 и запущен в 00:10 — вчерашние итоги приходят сразу,
    # а сегодняшние — в 23:30, как обычно
    t["now"] = day1 + timedelta(hours=4, minutes=10)
    assert await live.report_due()
    await live.build_report()
    assert await db.get_setting(live.REPORT_DATE_KEY) == day1.date().isoformat()
    assert await live.next_report_at() == slot + timedelta(days=1)
    assert not await live.report_due()

    # отчёт остановили кнопкой ⏹ — в этот день больше не пытаемся
    t["now"] = slot + timedelta(days=1, minutes=1)
    assert await live.report_due()
    await live.skip_pending()
    assert await live.next_report_at() == slot + timedelta(days=2)

    # время поменяли, пока шёл отчёт, — выбор пользователя остаётся в силе
    t["now"] = slot + timedelta(days=2, minutes=1)
    real = live.collect_day

    async def collect_and_change(job, now):
        await live.set_report_time(23, 59)
        return await real(job, now)

    monkeypatch.setattr(live, "collect_day", collect_and_change)
    await live.build_report()
    assert await live.next_report_at() == day1.replace(hour=23, minute=59) + timedelta(days=2)


# ------------------------------------------------- сведения о людях


async def test_person_info_in_two_steps(evening):
    from chat_parser.bot import live
    from chat_parser.config import settings
    from chat_parser.pii import author_hash, author_label

    h = evening
    await live.build_report(mark_sent=False)  # участники уже писали, их ники в базе

    prompt = await h.say("👤 Добавить инфо о человеке")
    assert prompt.startswith("👤 Информация о человеке") and "Пришли @username" in prompt
    out = await h.say("@ivan_optika")
    assert "👤 <b>Иван Петров (@ivan_optika)</b>" in out and "Теперь напиши, кто это" in out
    out = await h.say("Оптика Люкс, владелец, знакомы по выставке")
    assert "✅ Записал." in out and "Компания: Оптика Люкс" in out and "Роль: владелец" in out
    assert "Заметка: знакомы по выставке" in out
    assert "В отчётах: Иван Петров · Оптика Люкс, владелец" in out
    assert "по словам самого участника" not in out  # внесли сами — подсказка не нужна

    # сразу одним сообщением; не присланное не меняется, заметка дописывается
    out = await h.say("@IVAN_OPTIKA роль: продавец")
    assert "Компания: Оптика Люкс" in out and "Роль: продавец" in out
    out = await h.say("@ivan_optika заметка: закупает оправы у Essilor")
    assert "Заметка: знакомы по выставке\nзакупает оправы у Essilor" in out
    out = await h.say("/who_bbbbbbbb Линзы Плюс")
    assert "👤 <b>Мария</b>" in out and "Компания: Линзы Плюс" in out

    # нейросеть не ответила — описание целиком в заметке, ничего не потеряно
    h.state["person_fail"] = True
    await h.say("👤 Добавить инфо о человеке")
    await h.say("@olga")
    out = await h.say("ведёт приём в двух салонах по субботам")
    assert "нейросеть сейчас не ответила" in out
    assert "Заметка: ведёт приём в двух салонах по субботам" in out
    h.state["person_fail"] = False

    # ника нет в базе — ищу в Telegram; не нашёлся — жду другой ник
    await h.say("👤 Добавить инфо о человеке")
    out = await h.say("@nobody_here")
    assert "Не нашёл @nobody_here ни среди участников чатов, ни в Telegram" in out
    out = await h.say("@petr_lens")  # ещё не писал в чатах — нашёлся в Telegram
    assert "👤 <b>Пётр Смирнов (@petr_lens)</b>" in out and "Пока ничего не записано" in out
    out = await h.say("Линзы Центр, закупщик")
    assert "Компания: Линзы Центр" in out and "пока нет сообщений" in out
    label = author_label(author_hash(555, settings.author_salt))
    assert await h.pool.fetchval(
        "select role from authors where author_label = $1", label) == "закупщик"
    assert "чат или канал" in await h.say("@optika_news Новости")
    await h.say("👤 Добавить инфо о человеке")
    assert "Не вижу @username" in await h.say("Иван Петров из Оптики Люкс")

    assert "— Иван Петров · Оптика Люкс, продавец /who_aaaaaaaa" in await h.say("/signals 30")
    assert "Подключил чат" in await h.say("@optika_pro")  # один @ник вне подсказки — чат


@pytest.fixture
def channels_telegram(evening, monkeypatch):
    from types import SimpleNamespace

    from telethon.tl.types import (
        Message, MessageReactions, MessageReplies, PeerChannel, ReactionCount, ReactionEmoji,
    )

    from chat_parser import clock
    from chat_parser.bot import weekly

    h = evening
    h.calls.clear()
    h.album_reactions = 30
    now = weekly.slot_after(clock.now()) - timedelta(days=7)

    def message(mid, days, text, reactions, comments=None, *, album=None, post=True):
        replies = None if comments is None else MessageReplies(
            replies=comments, replies_pts=1, comments=True, channel_id=123,
        )
        return Message(
            id=mid, peer_id=PeerChannel(1234567890), date=now - timedelta(days=days),
            message=text, post=post, grouped_id=album, replies=replies, views=1000, forwards=3,
            reactions=MessageReactions(results=[
                ReactionCount(reaction=ReactionEmoji(emoticon="👍"), count=reactions),
            ]),
        )

    class Client:
        async def get_entity(self, ref):
            return SimpleNamespace(id=ref, broadcast=ref == PRO, username="optika_pro")

        async def disconnect(self):
            pass

        async def iter_messages(self, entity, offset_date):
            h.calls.append((entity.id, offset_date))
            yield message(7, 0, "Публикация после конца периода", 100)
            yield message(6, 1, "Ответ на пост", 90, post=False)
            yield message(5, 1, "", h.album_reactions, album=99)
            yield message(4, 1, "Как нашли оправы к сезону", h.album_reactions, album=99)
            yield message(3, 2, "Как меняли систему бонусов", 18, 29)
            yield message(2, 4, "Что поменяли в премиях продавцов", 12, 4)
            yield message(1, 8, "Пост из прошлой недели", 200)

    async def connect():
        return Client()

    monkeypatch.setattr(weekly, "connect_client", connect)
    return h


async def test_weekly_ranks_channel_posts_and_uses_pdf_only(channels_telegram):
    from chat_parser import clock, db
    from chat_parser.bot import main, weekly
    from chat_parser.report.channel_weekly import top_posts, top_themes

    h = channels_telegram
    await h.send_export()  # уже добавленная группа тоже видна в отчёте
    await h.pool.execute("update chats set in_daily = false where id = $1", PRO)
    await h.pool.execute(
        "insert into chats (id, title, is_active) values (-99, 'Старый канал', false)"
    )
    until = weekly.slot_after(clock.now()) - timedelta(days=7)
    await db.set_setting(weekly.NEXT_KEY, until.isoformat())

    report = await main.send_weekly(h.bot)
    assert report.until == until and report.since == until - timedelta(days=28)
    channels = {channel.title: channel for channel in report.channels}
    assert set(channels) == {"Оптики Про", "Оптики — обмен опытом", "Старый канал"}
    assert channels["Оптики — обмен опытом"].status.startswith("Группа")
    pro = channels["Оптики Про"]
    assert len(pro.posts) == 4
    assert [(p.id, p.reactions, p.comments) for p in top_posts(pro.posts)] == [
        (1, 200, None), (3, 18, 29), (4, 30, None),
    ]
    assert [(theme, len(posts)) for theme, posts in top_themes(pro)] == [
        ("Закупка оправ", 2),
    ]
    assert h.calls == [(PRO, until)]
    assert len(h.letters) == 1 and not h.session.files
    letter = h.letters[0]
    assert "Посты и темы каналов за четыре недели" in letter["Subject"]
    assert "Оптики Про" in letter.get_body(("plain",)).get_content()
    pdf = letter_pdf(letter)
    text = pdf_text(pdf)
    for part in ("Оптики Про", "Старый канал", "Публикаций за 4 недели 4",
                 "Реакции 18", "Комментарии 29", "Темы для своих публикаций",
                 "Как выбрать оправы к сезону", "Все публикации за четыре недели"):
        assert part in text, text
    assert "из прошлой недели" in text and "Ответ на пост" not in text
    assert "https://t.me/optika_pro/3" in pdf_links(pdf)
    assert await weekly.next_at() > clock.now()
    assert not await weekly.due()

    # Повторный отчёт для того же периода обновляет реакции уже опубликованных постов.
    h.album_reactions = 300
    updated = await weekly.build(until=until)
    updated_pro = next(ch for ch in updated.channels if ch.id == PRO)
    assert top_posts(updated_pro.posts)[0].id == 4


async def test_weekly_labels_imported_reactions_with_unknown_comments(harness):
    from chat_parser import clock
    from chat_parser.bot import weekly

    now = clock.now()
    await harness.pool.execute(
        "insert into chats (id, title, kind) values ($1, 'Импорт', 'public_channel')", PRO,
    )
    await harness.pool.execute(
        "insert into messages (chat_id, message_id, ts, text, reactions) "
        "values ($1, 1, $2, 'Новый подход к бонусам продавцов', 12)",
        PRO, now - timedelta(days=1),
    )
    report = await weekly.build(until=now)
    (channel,) = report.channels
    (post,) = channel.posts
    assert post.reactions == 12 and post.comments is None
    text = pdf_text(report.pdf())
    assert "Данные из импорта" in text and "н/д" in text


async def test_weekly_manual_pdf_uses_telegram_fallback_without_moving_slot(channels_telegram, monkeypatch):
    import smtplib

    from chat_parser import mailer
    from chat_parser.bot import weekly

    def broken(_mail):
        raise smtplib.SMTPAuthenticationError(535, b"invalid credentials")

    monkeypatch.setattr(mailer, "_send_sync", broken)
    h = channels_telegram
    scheduled = await weekly.next_at()
    result = await h.say("📅 Еженедельный отчёт")
    assert "✅ PDF отправлен сюда." in result
    assert h.letters == [] and len(h.session.files) == 1
    filename, data, caption = h.session.files[0]
    assert filename.endswith(".pdf") and data.startswith(b"%PDF") and caption is None
    assert await weekly.next_at() == scheduled


async def test_weekly_fanout_failure_keeps_due_slot(channels_telegram, monkeypatch):
    from chat_parser import clock, db
    from chat_parser.bot import delivery, main, weekly

    pending = weekly.slot_after(clock.now()) - timedelta(days=7)
    await db.set_setting(weekly.NEXT_KEY, pending.isoformat())

    async def failed_delivery(_bot, _chat_ids, _report):
        return None

    monkeypatch.setattr(delivery, "deliver", failed_delivery)
    with pytest.raises(RuntimeError, match="не доставлен"):
        await main.send_weekly(channels_telegram.bot)
    assert await weekly.next_at() == pending and await weekly.due()
    assert await db.get_setting(weekly.SENT_KEY) is None
