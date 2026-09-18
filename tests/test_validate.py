from chat_parser.analyze.schema import Extraction, Signal
from chat_parser.analyze.validate import validate

SOURCE = (
    "[m:101 | u:aa11 | 2026-02-03 11:20] чем меряете межзрачковое?\n"
    "[m:102 | u:bb22 | 2026-02-03 11:24] пупиллометром, но он у нас один на два "
    "салона, возим туда-сюда\n"
)


def sig(quote, ids=(102,), **kw):
    return Signal(
        type=kw.get("type", "pain"),
        audience="owner",
        summary="вожу пупиллометр между салонами",
        evidence_quote=quote,
        message_ids=list(ids),
        author_label="u:bb22",
        intensity=3,
        confidence=0.8,
        entities=["пупиллометр"],
        context="два салона, один прибор",
    )


def test_verbatim_quote_passes():
    ex = Extraction(has_signals=True, signals=[sig("он у нас один на два салона")])
    good, dropped = validate(ex, SOURCE, {101, 102})
    assert len(good) == 1 and dropped == 0


def test_whitespace_and_case_are_tolerated():
    ex = Extraction(has_signals=True, signals=[sig("Он У Нас   ОДИН на два салона")])
    good, dropped = validate(ex, SOURCE, {101, 102})
    assert len(good) == 1 and dropped == 0


def test_hallucinated_quote_is_dropped():
    ex = Extraction(
        has_signals=True,
        signals=[sig("мы купили второй пупиллометр в прошлом месяце")],
    )
    good, dropped = validate(ex, SOURCE, {101, 102})
    assert good == [] and dropped == 1


def test_paraphrase_is_dropped():
    """Пересказ вместо цитаты — тоже отбраковка."""
    ex = Extraction(has_signals=True, signals=[sig("прибор один на два салона и его возят")])
    good, dropped = validate(ex, SOURCE, {101, 102})
    assert good == [] and dropped == 1


def test_foreign_message_ids_are_stripped():
    ex = Extraction(
        has_signals=True, signals=[sig("он у нас один на два салона", ids=(102, 999))]
    )
    good, _ = validate(ex, SOURCE, {101, 102})
    assert good[0].message_ids == [102]


def test_signal_with_only_foreign_ids_is_dropped():
    ex = Extraction(has_signals=True, signals=[sig("он у нас один на два салона", ids=(999,))])
    good, dropped = validate(ex, SOURCE, {101, 102})
    assert good == [] and dropped == 1
