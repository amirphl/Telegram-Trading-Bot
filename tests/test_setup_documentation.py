import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from configs.config import ConfigValidationError
from telegram_trading_bot import cli

ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_check_config_is_a_read_only_runtime_shortcut(self):
        safe = SimpleNamespace(
            trading_mode="sandbox",
            enable_auto_execution=False,
            signal_extraction_enabled=False,
        )
        sys.modules.pop("internal.services.runner", None)
        output = io.StringIO()
        with patch.object(cli, "load_config", return_value=safe):
            with contextlib.redirect_stdout(output):
                result = cli.main(["--check-config"])

        self.assertEqual(result, 0)
        self.assertIn("Configuration is valid", output.getvalue())
        self.assertNotIn("internal.services.runner", sys.modules)

    def test_invalid_configuration_has_cli_error_without_traceback(self):
        stderr = io.StringIO()
        with patch.object(
            cli,
            "load_config",
            side_effect=ConfigValidationError(["API_ID is required"]),
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.main(["--check-config"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("API_ID is required", stderr.getvalue())


class PackagingDocumentationTests(unittest.TestCase):
    def test_packaging_declares_python_dependencies_and_console_script(self):
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.10,<3.15"', metadata)
        for dependency in ("ccxt==", "python-dotenv==", "Telethon=="):
            self.assertIn(dependency, requirements)
        self.assertIn('dependencies = { file = ["requirements.txt"] }', metadata)
        self.assertIn(
            'telegram-trading-bot = "telegram_trading_bot.cli:main"', metadata
        )

    def test_readme_and_safe_example_use_the_supported_command(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("telegram-trading-bot --check-config", readme)
        self.assertIn("telegram-trading-bot\n", readme)
        self.assertNotIn("yourusername", readme)
        self.assertNotIn("config.example.py", readme)
        self.assertIn("ENABLE_AUTO_EXECUTION=false", example)
        self.assertIn("TRADING_MODE=sandbox", example)
        self.assertNotIn("I_UNDERSTAND_REAL_MONEY_TRADING\n", example)


if __name__ == "__main__":
    unittest.main()
