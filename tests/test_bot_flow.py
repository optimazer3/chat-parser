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
from datetime import datetime, timezone

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
            return Card(title="t", statement="s", who="владельцы", when="при открытии",
                        current_workarounds=["возят прибор"], evidence=["цитата"],
                        product_hypotheses=["аренда"], open_questions=["сколько салонов"])
        raise AssertionError(schema)

    monkeypatch.setattr(LLM, "structured", fake_structured)
    export = _export_bytes()

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.mid = 1000
            self.sent: list[str] = []
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
        "drop table if exists runs, clusters, signals, threads, cursors, messages, chats cascade"
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


async def test_full_flow_through_bot(harness, monkeypatch):
    h = harness

    assert await h.say("/status", frm=999) == ""  # чужих молча игнорируем

    out = await h.send_export()
    assert "диалогов собрано: 3" in out
    assert "В очереди <b>3 треда</b>" in out and "[ex:all]" in out
    assert "Оценки расхода пока нет" in out

    out = await h.press("ex:all")
    n_signals = await h.pool.fetchval("select count(*) from signals")
    assert n_signals > 0
    assert f"сигналов: {n_signals}," in out
    assert "очередь пуста" in out
    assert "[cl]" in out

    out = await h.press("cl")
    assert "Болей: <b>1</b>" in out
    assert "Нехватка оборудования" in out

    out = await h.say("📊 Статус")
    assert f"Сигналов: <b>{n_signals}</b>" in out
    assert "ещё не синхронизирован" not in out  # импорт ставит время обновления

    cid = await h.pool.fetchval("select id from clusters")
    out = await h.say(f"/pain {cid}")
    assert "возят прибор" in out

    before = h.session.documents
    await h.say("📄 Отчёт")
    assert h.session.documents == before + 1

    out = await h.say("/redo")
    assert "[redo:yes]" in out
    out = await h.press("redo:yes")
    assert "Вернул в очередь 3 треда" in out
    assert "По прошлому прогону" in out  # оценка появилась после пробы

    assert "Упавших тредов нет" in await h.say("/retry")
    assert "выгрузка выключена" in await h.say("/addchat @x")

    # второй тяжёлый процесс не стартует, пока идёт первый
    async with h.jobs.exclusive("разбор сигналов"):
        assert "Сейчас идёт «разбор сигналов»" in await h.press("ex:all")

    # большая очередь -> /run сначала спрашивает
    from chat_parser.config import settings
    monkeypatch.setattr(settings, "bot_confirm_threshold", 1)
    out = await h.say("/run")
    assert "[run:all]" in out and "[run:20]" in out
    assert await h.pool.fetchval("select count(*) from threads where status='pending'") == 3
