from datetime import datetime, timedelta, timezone
from io import BytesIO

from pypdf import PdfReader

from chat_parser.ingest.channel_posts import Post
from chat_parser.report.channel_weekly import Channel, ChannelWeeklyReport


def test_every_page_identifies_channel_and_post_metrics_are_labelled():
    until = datetime(2026, 11, 30, 9, tzinfo=timezone(timedelta(hours=3)))
    channels = []
    for channel_id, name in enumerate(("Оптика. Первый канал", "Оборудование. Второй канал"), 1):
        posts = [Post(mid, until - timedelta(days=mid), f"Публикация {channel_id}-{mid}",
                      18, 3, views=1000, forwards=2, detail={"👍": 15, "🔥": 3})
                 for mid in range(1, 9)]
        channels.append(Channel(channel_id, name, None, posts))
    pdf = ChannelWeeklyReport(until - timedelta(days=28), until, channels).pdf()
    pages = PdfReader(BytesIO(pdf)).pages
    owners = []
    combined = []
    for page in pages:
        text = " ".join(page.extract_text().split())
        owner = [index for index, channel in enumerate(channels) if channel.title in text]
        assert len(owner) == 1, text
        owners.append(owner[0])
        headings = []

        def visit(content, _cm, _tm, font, size):
            if font and "Bold" in str(font.get("/BaseFont")) and size >= 20:
                headings.append(content)

        page.extract_text(visitor_text=visit)
        assert channels[owner[0]].title in " ".join(" ".join(headings).split())
        combined.append(text)
    assert owners == sorted(owners) and set(owners) == {0, 1}
    all_text = " ".join(combined)
    for label in ("Все реакции 18", "Лайки 15", "Комментарии 3", "Просмотры 1 000", "Репосты 2"):
        assert all_text.count(label) == 16
    for cid in (1, 2):
        for mid in range(1, 9):
            assert all_text.count(f"Публикация {cid}-{mid}") == 1
