"""Дымовой тест CLI: модуль импортируется и все команды регистрируются.

Ловит ошибки уровня модуля (например, константа объявлена ниже команды,
которая берёт её значением по умолчанию) — остальные тесты cli.py не трогают.
"""

from typer.testing import CliRunner

from chat_parser.cli import app

EXPECTED = {
    "doctor", "init-db", "models", "llm-test", "import-json", "add-chat",
    "ingest", "threads", "extract", "cluster", "report", "status", "bot", "pipeline",
}


def test_all_commands_registered():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    missing = {c for c in EXPECTED if c not in result.output}
    assert not missing, f"не видно команд: {missing}"


def test_each_command_help_renders():
    runner = CliRunner()
    for cmd in EXPECTED:
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd}: {result.output}"
