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
        Merging, PersonHint, Signal,
    )
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
            return DayDigest(highlights=["Поставщики срывают сроки, владельцы теряют клиентов",
                                         "Не хватает диагностических приборов"])
        if schema is Merging:
            return Merging(groups=[])
        if schema is Card:
            ids = [int(x) for x in re.findall(r"\[id (\d+)\]", user)][:3]
            return Card(title="t", statement="s", who="владельцы", when="при открытии",
                        current_workarounds=["возят прибор"], evidence_ids=ids,
                        product_hypotheses=["аренда"], open_questions=["сколько салонов"])
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
    await say("📄 Отчёт")
    assert h.session.documents == before + 1

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
    assert "Пришли ссылку на чат" in await h.say("➕ Добавить чат")

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
    assert "3 сообщения" in chats and "обсуждений: 1" in chats and "сообщ." not in chats
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
    """Аккаунт-сборщик «читает» чаты: сегодня в «Оптиках Про» пять сообщений."""
    from chat_parser.bot import live
    from chat_parser.config import settings
    from chat_parser.ingest import collector
    from chat_parser.people import upsert_authors

    h = harness
    monkeypatch.setattr(settings, "live_quiet_minutes", 0)
    monkeypatch.setattr(live, "connect_client", telegram_api_client)
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


async def test_evening_report(evening, monkeypatch):
    from chat_parser import clock
    from chat_parser.bot import live, main

    h = evening
    cid = await _old_pain(h)
    old_n = await h.pool.fetchval("select n_signals from clusters where id = $1", cid)
    assert "За последние 30 дней болей пока нет" in await h.say("🔝 Топ болей за месяц")

    late, early = clock.now().replace(hour=23, minute=0), clock.now().replace(hour=21)
    assert await live.report_due(late) and not await live.report_due(early)

    h.session.sent.clear()
    text = await main.send_report(h.bot)
    assert h.session.sent == [text]  # отчёт пришёл админу одним сообщением

    # переписка: чат без загруженной истории — сутки и сутки контекста, архивный — с курсора
    modes = {chat_id: (mode, since) for chat_id, mode, since in h.calls}
    assert modes[PRO][0] == "recent"
    assert timedelta(hours=47) < datetime.now(timezone.utc) - modes[PRO][1] < timedelta(hours=49)
    assert {m for m, _ in modes.values()} == {"recent", "incremental"}

    assert "📊 <b>Итоги дня ·" in text
    assert '<a href="https://t.me/optika_pro">Оптики Про</a>: 5 сообщений' in text
    assert "Оптики — обмен опытом</a>: сегодня тишина" in text
    assert "🧭 <b>Главное за день</b>" in text and "Поставщики срывают сроки" in text

    new_id = await h.pool.fetchval(
        "select id from clusters where label = 'Срыв сроков поставки'")
    new_part = text.split("🆕 <b>Новые боли</b>")[1].split("🔥")[0]
    assert f"<b>Срыв сроков поставки</b> — 2 человека, 2 сигнала → /pain_{new_id}" in new_part
    assert "Нехватка оборудования" not in new_part  # известная боль — не новая
    assert "↗ сообщение в чате" in new_part

    sharp = text.split("🔥 <b>Острые сигналы</b>")[1].split("📈")[0]
    assert "острота 5 из 5" in sharp and f"→ /pain_{cid}" in sharp
    assert "— Ольга Смирнова /who_cccccccc" in sharp
    assert '<a href="https://t.me/optika_pro/3">' in sharp
    assert sharp.count("острота") == 1  # остальное молча копится в базе

    assert "📈 <b>Всплески</b>" in text
    assert f"Нехватка оборудования — сегодня 3, раньше почти не упоминалась → /pain_{cid}" in text
    assert "Всего за день: 5 сигналов" in text
    assert "токен" not in text and "лимит" not in text and "пауз" not in text

    # известная боль сохранила номер и пополнилась, у новой есть описание
    assert await h.pool.fetchval("select n_signals from clusters where id = $1", cid) == old_n + 3
    assert "возят прибор" in await h.say(f"/pain_{new_id}")
    top = await h.say("🔝 Топ болей за месяц")
    assert "Нехватка оборудования" in top and "Срыв сроков поставки" in top

    # расход автоматического разбора отделён от ручного
    live_stages = {r["stage"] for r in await h.pool.fetch(
        "select distinct stage from llm_usage where source = 'live'")}
    assert live_stages == {"extract", "assign", "cards", "digest"}
    assert await h.pool.fetchval(
        "select count(*) from llm_usage where source = 'manual' and stage = 'assign'") == 0

    # отправлен — сегодня больше не придёт; архив из файла автоматически не трогается
    assert not await live.report_due(late)
    assert await h.pool.fetchval(
        "select count(*) from threads where chat_id <> $1 and status = 'pending'", PRO) == 0

    # «Итоги дня сейчас» из /live: считает с прошлого отчёта и не сбивает вечерний
    out = await h.say("/live")
    assert "включено" in out and "сегодняшние уже отправлены" in out and "[live:report]" in out
    assert "Следующие итоги: завтра в 22:00" in out
    await h.press("live:report")
    again = "\n".join(h.session.log[-1:])
    assert "Оптики Про</a>: сегодня тишина" in again and "Новых сигналов нет" in again
    assert [m for c, m, _ in h.calls if c == PRO] == ["recent", "incremental"]


async def test_evening_paused_and_budget(evening, monkeypatch):
    from chat_parser.bot import live
    from chat_parser.config import settings

    h = evening
    await h.send_export()  # архив не разбирали — вечером он не разбирается сам

    out = await h.press("live:off")
    assert "на паузе" in out
    text = await live.build_report()
    assert "⏸ Разбор на паузе" in text and "Оптики Про</a>: 5 сообщений" in text
    assert await h.pool.fetchval("select count(*) from signals") == 0
    assert await h.pool.fetchval("select count(*) from llm_usage where source = 'live'") == 0
    assert "В архиве ждут ручного разбора: 3 обсуждения" in text

    await h.press("live:on")
    monkeypatch.setattr(settings, "llm_price_in", 10.0)
    monkeypatch.setattr(settings, "llm_price_out", 10.0)
    await h.pool.execute("insert into llm_usage (stage, prompt_tokens, source) "
                         "values ('extract', 5000000, 'manual')")
    assert await live.budget_left() == pytest.approx(30.0)  # ручной разбор не в счёт
    await h.pool.execute("insert into llm_usage (stage, prompt_tokens, source) "
                         "values ('extract', 3100000, 'live')")
    h.inbox[PRO] = [("c", "срочно ищем мастера по ремонту оправ, никто не берётся", None)]
    text = await live.build_report()
    assert "⚠️ Дневной лимит на автоматический разбор исчерпан в" in text
    assert await h.pool.fetchval("select count(*) from signals") == 0
    assert await h.pool.fetchval(
        "select count(*) from threads where chat_id = $1 and status = 'pending'", PRO) >= 1
    out = await h.say("/live")
    assert "Потрачено сегодня: 31.00 из 30 ₽" in out and "лимит исчерпан" in out


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
    text = await live.build_report()
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
    report = await live.build_report(mark_sent=False)
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

    chats = await h.say("💬 Список чатов")
    assert f"[ppl:{PRO}] [del:{PRO}]" in chats and "[delreq:" in chats

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
    chats = await h.say("💬 Список чатов")
    assert "Оптики Про" not in chats and "Ждут подключения" not in chats


async def test_new_keyboard_is_sent_once(harness):
    from chat_parser.bot import main

    h = harness
    h.session.sent.clear()
    await main.announce_keyboard(h.bot)
    await main.announce_keyboard(h.bot)
    assert len(h.session.sent) == 1


# ---------------------------------------------------------- время отчёта


async def test_report_time(harness, monkeypatch):
    from chat_parser import clock, db
    from chat_parser.bot import live

    h = harness
    fixed = clock.now().replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(clock, "now", lambda: fixed)
    live.schedule_changed.clear()

    out = await h.say("⏰ Время отчёта")
    assert "приходят в <b>22:00</b> (МСК), следующие — сегодня в 22:00" in out
    assert "[rt:2100]" in out and "[rt:custom]" in out and "[cancel]" in out

    out = await h.press("rt:2100")
    assert "теперь приходят в <b>21:00</b> (МСК). Следующие — сегодня в 21:00." in out
    assert live.schedule_changed.is_set()  # цикл отчёта проснётся и пересчитает ожидание
    assert await live.report_time() == (21, 0)
    assert not await live.report_due(fixed)
    assert await live.report_due(fixed.replace(hour=21, minute=1))
    await h.say("⏰ Время отчёта")
    assert "✓ 21:00" in h.session.labels and "22:00" in h.session.labels

    prompt = await h.press("rt:custom")
    assert prompt.startswith("⏰ Во сколько присылать итоги дня?")
    out = await h.say("25:00")
    assert "Не понял «25:00»" in out
    out = await h.say("21:45")  # после ошибки бот ждёт ответ снова
    assert "<b>21:45</b>" in out and "сегодня в 21:45" in out

    # время уже прошло, а сегодняшних итогов не было — предлагаем прислать сейчас
    out = await h.say("9", reply_to=prompt)
    assert "Следующие — завтра в 9:00" in out and "9:00 сегодня уже прошло" in out
    assert "[live:catchup]" in out
    out = await h.press("live:catchup")
    assert "📊 <b>Итоги дня" in out
    assert await db.get_setting(live.REPORT_DATE_KEY) == fixed.date().isoformat()
    tomorrow_9 = fixed.replace(hour=9) + timedelta(days=1)
    assert await live.next_report_at() == tomorrow_9  # расписание не сдвинулось

    # сегодняшние уже пришли — более позднее время сегодня второго отчёта не даст
    out = await h.press("rt:2300")
    assert "Следующие — завтра в 23:00" in out and "уже прошло" not in out
    assert "Каждый день в <b>23:00</b> (МСК)" in await h.say("/help")


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

    # время поменяли на более позднее, пока шёл отчёт, — второго в тот же день нет
    t["now"] = slot + timedelta(days=2, minutes=1)
    real = live.collect_day

    async def collect_and_change(job, now):
        await live.set_report_time(23, 59)
        return await real(job, now)

    monkeypatch.setattr(live, "collect_day", collect_and_change)
    await live.build_report()
    assert await live.next_report_at() == day1.replace(hour=23, minute=59) + timedelta(days=3)


async def test_report_loop_wakes_on_new_time(harness, monkeypatch):
    import asyncio

    from chat_parser import clock, db
    from chat_parser.bot import live, main

    sent = []

    async def fake_send(bot, mark_sent=True):
        sent.append(clock.now())
        await live.skip_pending()

    monkeypatch.setattr(main, "send_report", fake_send)
    fixed = clock.now().replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(clock, "now", lambda: fixed)
    await live.set_report_time(21, 0)

    loop = asyncio.create_task(main.report_loop(harness.bot))
    try:
        await asyncio.sleep(0.2)
        assert sent == []  # до 21:00 ещё шесть часов — цикл спит
        # время «наступило»: так бывает, если его перенесли на уже прошедшее
        await db.set_setting(live.NEXT_REPORT_KEY, (fixed - timedelta(minutes=1)).isoformat())
        live.schedule_changed.set()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if sent:
                break
        assert len(sent) == 1
    finally:
        loop.cancel()


# ------------------------------------------------------ сведения о людях


async def test_person_info_by_username(evening):
    from chat_parser.bot import live
    from chat_parser.config import settings
    from chat_parser.pii import author_hash, author_label

    h = evening
    await live.build_report(mark_sent=False)  # участники уже писали, их ники в базе

    prompt = await h.say("👤 Инфо о человеке")
    assert prompt.startswith("👤 Информация о человеке") and "@ivan_optika Оптика Люкс" in prompt
    out = await h.say("@ivan_optika Оптика Люкс, владелец, знакомы по выставке")
    assert "✅ Сохранил." in out and "👤 <b>Иван Петров</b> (@ivan_optika)" in out
    assert "Компания: Оптика Люкс" in out and "Роль: владелец" in out
    assert "Заметка: знакомы по выставке" in out
    assert "по словам самого участника" not in out  # внесли сами — подсказка не нужна

    # и без ответа на подсказку; регистр ника не важен; не присланное не меняется
    out = await h.say("@IVAN_OPTIKA роль: продавец")
    assert "Компания: Оптика Люкс" in out and "Роль: продавец" in out
    assert "Заметка: знакомы по выставке" in out
    out = await h.say("/who_bbbbbbbb Линзы Плюс")
    assert "👤 <b>Мария</b>" in out and "Компания: Линзы Плюс" in out
    await h.say("👤 Инфо о человеке")
    out = await h.say("@olga")  # в ответ на подсказку один ник — просто карточка
    assert "Ольга Смирнова" in out and "Сохранил" not in out
    await h.say("👤 Инфо о человеке")
    assert "Не вижу @username" in await h.say("Иван Петров, Оптика Люкс")
    out = await h.say("@maria_opt Линзы Плюс")  # после подсказки об ошибке ждём снова
    assert "Не нашёл @maria_opt" in out

    # ещё не писал в чатах — нашёлся в Telegram, сведения записаны заранее
    out = await h.say("@petr_lens Линзы Центр, закупщик")
    assert "👤 <b>Пётр Смирнов</b> (@petr_lens)" in out and "Компания: Линзы Центр" in out
    assert "пока нет сообщений" in out
    label = author_label(author_hash(555, settings.author_salt))
    assert await h.pool.fetchval(
        "select role from authors where author_label = $1", label) == "закупщик"
    out = await h.say("@nobody_here Оптика")
    assert "Не нашёл @nobody_here ни среди участников чатов, ни в Telegram" in out
    assert "чат или канал" in await h.say("@optika_news Новости")

    assert "— Иван Петров · Оптика Люкс, продавец /who_aaaaaaaa" in await h.say("/signals 30")
    # один @ник без текста — по-прежнему добавление чата
    assert "Подключил чат" in await h.say("@optika_pro")


async def test_prompts_keep_keyboard_and_can_be_left(harness, monkeypatch):
    """Бот ждёт ответ на подсказку, но кнопки внизу работают как обычно:
    нажал другую кнопку, команду или «Отмена» — ждать перестаёт."""
    from chat_parser.bot import handlers, live

    h = harness
    await h.press("rt:custom")
    assert "Чатов пока нет" in await h.say("💬 Список чатов")  # кнопка сработала как обычно
    assert await h.say("21:30") == ""  # это уже не ответ на подсказку
    assert await live.report_time() == (22, 0)

    await h.press("rt:custom")
    await h.say("/status")
    assert await h.say("21:30") == ""

    await h.press("rt:custom")
    await h.press("cancel")
    assert await h.say("21:30") == ""

    await h.press("rt:custom")
    monkeypatch.setattr(handlers, "AWAIT_SECONDS", -1)  # прошло больше 15 минут
    await h.press("rt:custom")
    assert await h.say("21:30") == ""

    monkeypatch.setattr(handlers, "AWAIT_SECONDS", 900)
    await h.press("rt:custom")
    assert "<b>21:30</b>" in await h.say("21:30")
    assert await live.report_time() == (21, 30)
