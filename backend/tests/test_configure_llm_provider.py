import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.scripts.configure_llm_provider import (
    build_noninteractive_updates,
    is_llm_configured,
    read_env_values,
    write_env_updates,
)


class ConfigureLLMProviderTest(unittest.TestCase):
    def test_write_env_updates_preserves_unrelated_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "EXISTING=yes\n"
                "LLM_PROVIDER=deepseek\n"
                "# keep this comment\n",
                encoding="utf-8",
            )

            write_env_updates(
                env_path,
                {
                    "LLM_PROVIDER": "kimi",
                    "LLM_API_KEY": "sk key#1",
                    "LLM_MODEL": "kimi-k2.6",
                    "LLM_BASE_URL": "https://api.moonshot.cn/v1",
                    "LLM_TIMEOUT_SECONDS": "60",
                    "LLM_MAX_RETRIES": "1",
                    "LLM_IGNORE_PROXY": "1",
                },
            )

            content = env_path.read_text(encoding="utf-8")
            values = read_env_values(env_path)

        self.assertIn("EXISTING=yes", content)
        self.assertIn("# keep this comment", content)
        self.assertIn("LLM_PROVIDER=kimi", content)
        self.assertIn("LLM_IGNORE_PROXY=1", content)
        self.assertEqual(values["LLM_API_KEY"], "sk key#1")
        self.assertEqual(values["LLM_BASE_URL"], "https://api.moonshot.cn/v1")

    def test_noninteractive_local_provider_does_not_require_api_key(self) -> None:
        updates = build_noninteractive_updates(
            SimpleNamespace(
                provider="local",
                api_key=None,
                model=None,
                base_url=None,
            )
        )

        self.assertEqual(updates["LLM_PROVIDER"], "local")
        self.assertEqual(updates["LLM_API_KEY"], "")
        self.assertEqual(updates["LLM_MODEL"], "llama3.1")
        self.assertEqual(updates["LLM_IGNORE_PROXY"], "1")

    def test_noninteractive_cloud_provider_requires_api_key(self) -> None:
        with self.assertRaises(ValueError):
            build_noninteractive_updates(
                SimpleNamespace(
                    provider="kimi",
                    api_key=None,
                    model=None,
                    base_url=None,
                )
            )

    def test_configured_detection(self) -> None:
        self.assertFalse(is_llm_configured({"LLM_PROVIDER": "deepseek"}))
        self.assertTrue(
            is_llm_configured(
                {
                    "LLM_PROVIDER": "deepseek",
                    "LLM_API_KEY": "sk-test",
                }
            )
        )
        self.assertTrue(is_llm_configured({"LLM_PROVIDER": "local"}))


if __name__ == "__main__":
    unittest.main()
