from datetime import datetime, timedelta, timezone

from chat_parser.normalize.threads import assign_threads, is_worth_analyzing, thread_hash

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def msg(mid, minutes, author, text="норм текст сюда", reply=None):
    return {
        "message_id": mid,
        "ts": T0 + timedelta(minutes=minutes),
        "author_label": author,
        "text": text,
        "reply_to": reply,
    }


def test_reply_chain_forms_one_thread():
    rows = [
        msg(1, 0, "u:a"),
        msg(2, 120, "u:b", reply=1),   # реплай через 2 часа всё равно в тот же тред
        msg(3, 121, "u:c", reply=2),
    ]
    threads = assign_threads(rows)
    assert len(threads) == 1
    assert threads[0].ids == [1, 2, 3]


def test_time_gap_starts_new_thread():
    rows = [msg(1, 0, "u:a"), msg(2, 2, "u:b"), msg(3, 90, "u:c")]
    threads = assign_threads(rows)
    assert [t.ids for t in threads] == [[1, 2], [3]]


def test_parallel_conversations_split_by_author():
    """Два разговора идут одновременно: автор возвращается в свой тред."""
    rows = [
        msg(1, 0, "u:a"),
        msg(2, 1, "u:b", reply=1),
        msg(10, 2, "u:x"),            # новый разговор — активных два, автор новый
        msg(11, 3, "u:a"),            # u:a уже был в треде 1 -> туда
    ]
    threads = assign_threads(rows)
    by_root = {t.root_id: t.ids for t in threads}
    assert 11 in by_root[1]


def test_thread_is_capped_and_closed():
    from chat_parser.config import settings

    n = settings.thread_max_messages
    rows = [msg(i, i * 0.1, f"u:{i % 3}") for i in range(1, n + 6)]
    threads = assign_threads(rows)
    assert len(threads) >= 2
    assert all(len(t.ids) <= n for t in threads)


def test_noise_is_filtered_but_long_standalone_survives():
    short = assign_threads([msg(1, 0, "u:a", "+")])[0]
    assert not is_worth_analyzing(short, {1: "+"})

    text = "подскажите чем полировать полимерные линзы, паста царапает покрытие"
    long_one = assign_threads([msg(1, 0, "u:a", text)])[0]
    assert is_worth_analyzing(long_one, {1: text})


def test_hash_changes_when_thread_grows():
    h1 = thread_hash([1, 2], {1: "а", 2: "б"})
    h2 = thread_hash([1, 2, 3], {1: "а", 2: "б", 3: "в"})
    assert h1 != h2
