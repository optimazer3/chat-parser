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


class Extraction(BaseModel):
    has_signals: bool
    signals: list[Signal]


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
