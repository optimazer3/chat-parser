import pytest

from chat_parser.analyze import extract as extract_mod
from chat_parser.analyze.schema import Extraction
from chat_parser.config import settings
from chat_parser.llm import TokenBudgetExhausted


class FakeLLM:
    def __init__(self, fail_first: bool):
        self.fail_first = fail_first
        self.budgets: list[int] = []

    async def structured(self, system, user, schema, max_tokens):
        self.budgets.append(max_tokens)
        if self.fail_first and len(self.budgets) == 1:
            raise TokenBudgetExhausted("рассуждения съели лимит")
        return Extraction(has_signals=False, signals=[])


@pytest.mark.asyncio
async def test_heavy_thread_retried_with_bigger_budget():
    llm = FakeLLM(fail_first=True)
    await extract_mod.extract_one(llm, "текст")
    assert llm.budgets == [settings.extract_max_tokens, settings.extract_max_tokens_retry]


@pytest.mark.asyncio
async def test_normal_thread_uses_normal_budget_once():
    llm = FakeLLM(fail_first=False)
    await extract_mod.extract_one(llm, "текст")
    assert llm.budgets == [settings.extract_max_tokens]


def test_summary_estimates_remaining_queue():
    stats = {
        "threads": 19, "signals": 43, "dropped": 1, "empty": 6, "failed": 1,
        "drop_rate": 0.023, "seconds": 192,
        "tokens": {"calls": 21, "prompt": 60_000, "completion": 40_000, "reasoning": 30_000},
        "pending_left": 500,
    }
    text = "\n".join(extract_mod.summary_lines(stats))
    assert "3 мин 12 с" in text
    assert "43" in text and "2.3%" in text
    assert "рассуждения 30 000" in text
    assert "5 000 токенов на тред" in text          # 100 000 / 20 тредов
    assert "2 500 000 токенов на всё" in text       # 5 000 * 500
    assert "--retry-failed" in text


def test_summary_without_usage_does_not_crash():
    stats = {"threads": 0, "signals": 0, "dropped": 0, "empty": 0, "failed": 0,
             "drop_rate": 0.0, "seconds": 0, "tokens": {}, "pending_left": 0}
    assert "очередь пуста" in "\n".join(extract_mod.summary_lines(stats))
