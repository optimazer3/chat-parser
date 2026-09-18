"""Маскирование персональных данных и псевдонимизация авторов.

Применяется на этапе записи в БД: реальные user_id и контакты в хранилище
не попадают вообще. Это и требование 152-ФЗ/GDPR по минимизации, и защита
от того, чтобы контакты уехали в LLM.
"""

from __future__ import annotations

import hashlib
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")
# Телефоны в российском/международном формате. Намеренно узкий шаблон,
# чтобы не съедать цены и артикулы («линзы 12000», «SPH -2.75»).
PHONE_RE = re.compile(
    r"(?<![\w\d])(?:\+7|\+\d{1,3}|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?![\w\d])"
)


def mask_text(text: str | None) -> str:
    if not text:
        return ""
    text = EMAIL_RE.sub("<EMAIL>", text)
    text = CARD_RE.sub("<CARD>", text)
    text = PHONE_RE.sub("<PHONE>", text)
    return text


def author_hash(user_id: int | None, salt: str) -> str | None:
    if user_id is None:
        return None
    return hashlib.sha256(f"{salt}:{user_id}".encode()).hexdigest()[:16]


def author_label(h: str | None) -> str | None:
    return f"u:{h[:8]}" if h else None
