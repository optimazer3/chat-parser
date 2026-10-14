from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SignalType = Literal[
    "pain",               # прямая жалоба на затруднение
    "need",               # сформулированная потребность («нужен способ…»)
    "jtbd",               # задача, которую человек пытается решить
    "question",           # повторяющийся вопрос = пробел в рынке/сервисе
    "workaround",         # костыль, которым пользуются сейчас
    "alternative",        # упомянутый продукт/поставщик/конкурент как замена
    "willingness_to_pay", # разговор про деньги, цену, готовность платить
    "feature_request",    # «вот бы существовало…»
]

Audience = Literal[
    "owner",        # владелец/управляющий оптики
    "staff",        # продавец-консультант, администратор салона
    "optometrist",  # врач-офтальмолог, оптометрист
    "supplier",     # поставщик линз/оправ/оборудования
    "customer",     # покупатель очков/линз
    "unknown",
]


class Signal(BaseModel):
    type: SignalType
    audience: Audience
    summary: str = Field(description="Формулировка от первого лица, до 140 символов")
    evidence_quote: str = Field(description="Дословная цитата из чанка, без изменений")
    message_ids: list[int]
    author_label: str
    intensity: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    entities: list[str] = Field(description="Бренды, поставщики, оборудование, ПО")
    context: str = Field(description="Обстоятельства, в которых возникает проблема")


class PersonHint(BaseModel):
    author_label: str
    company: str = Field(description="Компания или салон, если человек сам назвал; иначе пусто")
    role: str = Field(
        description="Кем работает: владелец, продавец, оптометрист, поставщик…; иначе пусто"
    )
    evidence_quote: str = Field(description="Дословная цитата, где человек говорит о себе")


class Extraction(BaseModel):
    has_signals: bool
    signals: list[Signal]
    # Кто есть кто — по словам самих людей. Становится подсказкой в карточке
    # участника, пока пользователь не подтвердит или не впишет своё.
    people: list[PersonHint] = Field(default_factory=list)


class ClusterDraft(BaseModel):
    label: str = Field(description="Короткое имя кластера, до 60 символов")
    statement: str = Field(description="Каноническая формулировка боли от первого лица")
    signal_ids: list[int]


class Clustering(BaseModel):
    clusters: list[ClusterDraft]


class MergeGroup(BaseModel):
    label: str
    statement: str
    draft_ids: list[int]


class Merging(BaseModel):
    groups: list[MergeGroup]


class Card(BaseModel):
    title: str
    statement: str
    who: str = Field(description="Кто испытывает эту боль")
    when: str = Field(description="В какой момент она возникает")
    current_workarounds: list[str]
    # Номера сигналов, а не текст: у сигнала цитата проверена и привязана
    # к сообщению — под ней можно дать ссылку.
    evidence_ids: list[int] = Field(
        description="id 3-5 сигналов с самыми показательными цитатами"
    )
    product_hypotheses: list[str]
    open_questions: list[str]


class AssignItem(BaseModel):
    signal_id: int
    pain_id: int = Field(description="id подходящей боли или 0, если ни одна не подходит")


class Assignment(BaseModel):
    items: list[AssignItem]


class DayDigest(BaseModel):
    summary: str = Field(default="", description="Обзор суток связным текстом, 3-5 предложений")
    highlights: list[str] = Field(description="5-7 главных выводов, по одному предложению")
    opportunities: list[str] = Field(
        default_factory=list,
        description="2-4 возможности для бизнеса, прямо вытекающие из сигналов; пусто, если нет",
    )


class PostVerdict(BaseModel):
    id: int
    is_post: bool = Field(description="пост — автор раскрывает тему; иначе реплика или вопрос")
    theme: str = Field(description="тема поста в 2-5 словах; для реплик — пусто")
    summary: str = Field(description="о чём пост, одной фразой; для реплик — пусто")


class PostBatch(BaseModel):
    items: list[PostVerdict]


class WeeklyDigest(BaseModel):
    summary: str = Field(description="что откликается за неделю, месяц и полгода, 3-5 предложений")
    focus: list[str] = Field(description="3-6 выводов о темах фокусного автора")
    others: list[str] = Field(default_factory=list,
                              description="2-4 вывода об интересном у других участников")
    ideas: list[str] = Field(default_factory=list,
                             description="3-5 тем для будущих постов из того, что откликается")


class PersonAbout(BaseModel):
    """Описание участника, которое прислал пользователь, — по полям."""

    company: str = Field(description="Компания, салон или сеть; пусто, если не названа")
    role: str = Field(description="Кем работает или кто это; пусто, если не сказано")
    details: str = Field(description="Остальные факты из описания, кратко; пусто, если нет")
