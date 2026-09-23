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
        Card, ClusterDraft, Clustering, Extraction, Merging, Signal,
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

    async def fake_structured(self, system, user, schema, max_tokens=8000):
        self.usage["calls"] += 1
        self.usage["prompt"] += 900
        self.usage["completion"] += 600
        if schema is Extraction:
            sigs = [
                Signal(type="pain", audience="owner", summary=m.group(3)[:100],
                       evidence_quote=" ".join(m.group(3).split()[:5]),
                       message_ids=[int(m.group(1))], author_label=m.group(2),
                       intensity=3, confidence=0.9, entities=[], context="тест")
                for m in map(LINE.match, user.splitlines())
                if m and len(m.group(3).split()) >= 5
            ]
            return Extraction(has_signals=bool(sigs), signals=sigs)
        if schema is Clustering:
            ids = [int(x) for x in re.findall(r"^(\d+)\t", user, re.M)]
            return Clustering(clusters=[ClusterDraft(
                label="Нехватка оборудования", statement="мне не хватает приборов",
                signal_ids=ids)])
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
                if rm is not None and hasattr(rm, "inline_keyboard"):
                    text += " " + " ".join(
                        f"[{b.callback_data}]" for row in rm.inline_keyboard for b in row
                    )
                self.sent.append(text)
                self.log.append(text)
                if name == "SendMessage":
                    self.mid += 1
                    return self._msg(bot, self.mid, method.text)
                return self._msg(bot, method.message_id, method.text)
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

    pool = await db.get_pool()
    await pool.execute(
        "drop table if exists runs, clusters, signals, threads, cursors, messages, chats, "
        "llm_usage, chat_requests cascade"
    )
    await db.apply_schema()

    class H:
        pass

    h = H()
    h.session, h.pool, h.jobs = session, pool, jobs
    h.counter = 0

    async def feed(update_kwargs):
        from aiogram.types import Update
        h.counter += 1
        session.sent.clear()
        await dp.feed_update(bot, Update(update_id=h.counter, **update_kwargs))
        return "\n".join(session.sent)

    def msg(text=None, frm=ADMIN, **kw):
        user = User(id=frm, is_bot=False, first_name="u")
        return {"message": Message(
            message_id=h.counter + 1, date=datetime.now(timezone.utc),
            chat=Chat(id=ADMIN, type="private"), from_user=user, text=text, **kw)}

    async def say(text, frm=ADMIN):
        return await feed(msg(text, frm))

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

    class FakeClient:
        async def disconnect(self):
            pass

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
