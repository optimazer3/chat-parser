from chat_parser.pii import author_hash, author_label, mask_text


def test_masks_contacts():
    text = "пишите на info@optika.ru или +7 (912) 345-67-89, оплата 4111 1111 1111 1111"
    out = mask_text(text)
    assert "<EMAIL>" in out and "<PHONE>" in out and "<CARD>" in out
    assert "optika.ru" not in out and "345-67-89" not in out


def test_keeps_prices_and_diopters():
    """Цены и оптические параметры не должны попадать под маску телефона."""
    text = "оправа 12000, линзы 8500, рецепт SPH -2.75 CYL -0.5, межзрачковое 64"
    assert mask_text(text) == text


def test_author_pseudonym_is_stable_and_salted():
    a = author_hash(123456, "salt-a")
    b = author_hash(123456, "salt-b")
    assert a == author_hash(123456, "salt-a")
    assert a != b
    assert author_label(a).startswith("u:") and len(author_label(a)) == 10
    assert author_hash(None, "salt-a") is None


def test_mask_is_idempotent():
    once = mask_text("почта a@b.ru и телефон +7 912 345 67 89")
    assert mask_text(once) == once
