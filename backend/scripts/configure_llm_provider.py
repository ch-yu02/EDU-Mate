"""Interactive first-run helper for backend LLM provider configuration.

The application still reads provider settings from environment variables at
runtime. This script only gives first-time users a guided way to create or
update ``.env`` with one of the supported OpenAI-compatible templates.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = ROOT_DIR / ".env"

LLM_KEYS = (
    "LLM_PROVIDER",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
)

NO_PROXY_VALUE = "localhost,127.0.0.1"


@dataclass(frozen=True)
class LLMTemplate:
    """A provider preset that maps to backend LLM environment variables."""

    template_id: str
    label: str
    provider: str
    model: str
    base_url: str
    notes: str
    requires_api_key: bool = True


TEMPLATES: tuple[LLMTemplate, ...] = (
    LLMTemplate(
        template_id="kimi",
        label="Kimi / Moonshot",
        provider="kimi",
        model="kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
        notes="适合中文课堂与图片分析，项目会自动把 Kimi temperature 规整为 1。",
    ),
    LLMTemplate(
        template_id="deepseek",
        label="DeepSeek V4",
        provider="deepseek",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        notes="适合文本图谱、问答和课后产物；图片分析需要另配支持视觉的模型。",
    ),
    LLMTemplate(
        template_id="openai",
        label="OpenAI",
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        notes="适合文本与多模态能力统一接入。",
    ),
    LLMTemplate(
        template_id="qwen",
        label="通义千问 / DashScope",
        provider="openai_compatible",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        notes="使用 DashScope OpenAI-compatible endpoint。",
    ),
    LLMTemplate(
        template_id="local",
        label="本地 OpenAI-compatible",
        provider="local",
        model="llama3.1",
        base_url="http://127.0.0.1:11434/v1",
        notes="用于 Ollama、vLLM、llama.cpp server 等本地服务，可不填 API key。",
        requires_api_key=False,
    ),
)


def main(argv: list[str] | None = None) -> int:
    """Run the first-run LLM configuration helper."""
    parser = argparse.ArgumentParser(
        description="Configure EDU-Mate backend LLM provider in .env."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help="Path to the .env file to read/update.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check whether an enabled provider is configured.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run configuration even when an enabled provider already exists.",
    )
    parser.add_argument(
        "--print-templates",
        action="store_true",
        help="Print provider templates and exit.",
    )
    parser.add_argument(
        "--provider",
        choices=[template.template_id for template in TEMPLATES] + ["custom"],
        help="Non-interactive template id.",
    )
    parser.add_argument("--api-key", help="Non-interactive API key value.")
    parser.add_argument("--model", help="Override template model.")
    parser.add_argument("--base-url", help="Override template base URL.")
    args = parser.parse_args(argv)

    if args.print_templates:
        print_templates()
        return 0

    env_file: Path = args.env_file
    values = read_env_values(env_file)
    merged_values = values | {
        key: value for key in LLM_KEYS if (value := os.getenv(key)) is not None
    }

    if args.check:
        if is_llm_configured(merged_values):
            provider = merged_values.get("LLM_PROVIDER", "unknown")
            model = merged_values.get("LLM_MODEL", "")
            print(f"LLM provider is configured: {provider} {model}".strip())
            return 0
        print("LLM provider is not configured.")
        return 1

    if not args.force and is_llm_configured(merged_values):
        provider = merged_values.get("LLM_PROVIDER", "unknown")
        model = merged_values.get("LLM_MODEL", "")
        print(f"LLM provider already configured: {provider} {model}".strip())
        print("Use scripts/dev.sh llm-config --force to reconfigure.")
        return 0

    if args.provider:
        try:
            updates = build_noninteractive_updates(args)
        except ValueError as exc:
            print(f"LLM config failed: {exc}", file=sys.stderr)
            return 2
    else:
        if not sys.stdin.isatty():
            print("LLM provider is missing and no interactive terminal is available.")
            print("Configure .env manually or run: scripts/dev.sh llm-config")
            return 2
        updates = prompt_for_updates(values)

    write_env_updates(env_file, updates)
    print(f"Updated LLM provider configuration in {env_file}")
    print(
        "Direct environment variables still take precedence when you run a command "
        "with LLM_PROVIDER/LLM_API_KEY/etc. already exported."
    )
    return 0


def print_templates() -> None:
    """Print available provider templates."""
    for index, template in enumerate(TEMPLATES, start=1):
        api_key_hint = "需要 API key" if template.requires_api_key else "API key 可留空"
        print(f"{index}. {template.template_id} - {template.label}")
        print(f"   provider={template.provider}")
        print(f"   model={template.model}")
        print(f"   base_url={template.base_url}")
        print(f"   {api_key_hint}；{template.notes}")


def read_env_values(env_file: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs from a dotenv-style file."""
    if not env_file.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_assignment(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def is_llm_configured(values: dict[str, str]) -> bool:
    """Return whether the current values enable a provider."""
    provider = values.get("LLM_PROVIDER", "deepseek").strip().lower() or "deepseek"
    api_key = values.get("LLM_API_KEY", "").strip()
    base_url = values.get("LLM_BASE_URL", "").strip()
    if provider == "local":
        return True
    return bool(api_key)


def build_noninteractive_updates(args: argparse.Namespace) -> dict[str, str]:
    """Build .env updates from CLI flags."""
    template = _template_by_id(args.provider)
    if template is None and args.provider != "custom":
        raise ValueError(f"unknown provider template: {args.provider}")

    provider = args.provider if args.provider == "custom" else template.provider
    model = args.model or (template.model if template is not None else "")
    base_url = args.base_url or (template.base_url if template is not None else "")
    requires_key = True if template is None else template.requires_api_key
    api_key = args.api_key or ""

    if requires_key and not api_key.strip():
        raise ValueError("--api-key is required for this provider")
    if not model.strip():
        raise ValueError("--model is required")
    if not base_url.strip():
        raise ValueError("--base-url is required")

    return _settings_updates(provider, api_key, model, base_url)


def prompt_for_updates(current_values: dict[str, str]) -> dict[str, str]:
    """Ask the user which provider template should be written to .env."""
    print("首次运行需要配置后端云端/本地 LLM。")
    print("API key 只会写入本机 .env，前端不会读取或打包它。")
    print()
    print_templates()
    print()

    template = _prompt_template()
    model = _prompt_with_default("模型名称", template.model)
    base_url = _prompt_with_default("OpenAI-compatible Base URL", template.base_url)
    api_key = ""
    if template.requires_api_key:
        api_key = _prompt_secret("API key")
    else:
        api_key = _prompt_with_default("API key（本地服务可留空）", "")

    existing_timeout = current_values.get("LLM_TIMEOUT_SECONDS", "60")
    timeout = _prompt_with_default("单次请求超时时间秒数", existing_timeout)
    existing_retries = current_values.get("LLM_MAX_RETRIES", "1")
    retries = _prompt_with_default("失败重试次数", existing_retries)

    updates = _settings_updates(template.provider, api_key, model, base_url)
    updates["LLM_TIMEOUT_SECONDS"] = timeout
    updates["LLM_MAX_RETRIES"] = retries
    return updates


def write_env_updates(env_file: Path, updates: dict[str, str]) -> None:
    """Update or append selected .env variables, preserving unrelated lines."""
    existing_lines = (
        env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    )
    remaining = dict(updates)
    written_lines: list[str] = []

    for line in existing_lines:
        parsed = _parse_env_assignment(line)
        if parsed is None:
            written_lines.append(line)
            continue

        key, _value = parsed
        if key in remaining:
            written_lines.append(format_env_line(key, remaining.pop(key)))
        else:
            written_lines.append(line)

    if remaining:
        if written_lines and written_lines[-1].strip():
            written_lines.append("")
        if not any("EDU-Mate LLM provider" in line for line in written_lines):
            written_lines.append("# EDU-Mate LLM provider")
        for key in LLM_KEYS:
            if key in remaining:
                written_lines.append(format_env_line(key, remaining.pop(key)))
        for key, value in remaining.items():
            written_lines.append(format_env_line(key, value))

    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(written_lines).rstrip() + "\n", encoding="utf-8")


def format_env_line(key: str, value: str) -> str:
    """Format one shell-sourceable dotenv line."""
    return f"{key}={_shell_quote(value)}"


def _settings_updates(
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
) -> dict[str, str]:
    """Build the common LLM settings block."""
    return {
        "LLM_PROVIDER": provider.strip(),
        "LLM_API_KEY": api_key.strip(),
        "LLM_MODEL": model.strip(),
        "LLM_BASE_URL": base_url.strip().rstrip("/"),
        "LLM_TIMEOUT_SECONDS": "60",
        "LLM_MAX_RETRIES": "1",
        "NO_PROXY": NO_PROXY_VALUE,
        "no_proxy": NO_PROXY_VALUE,
    }


def _prompt_template() -> LLMTemplate:
    """Prompt for a provider template."""
    while True:
        answer = input(f"请选择模板 [1-{len(TEMPLATES)}]，默认 1: ").strip() or "1"
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(TEMPLATES):
                return TEMPLATES[index - 1]
        template = _template_by_id(answer)
        if template is not None:
            return template
        print("输入无效，请输入序号或模板 id。")


def _prompt_with_default(label: str, default: str) -> str:
    """Prompt for a value with a default."""
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def _prompt_secret(label: str) -> str:
    """Prompt for a non-empty secret value."""
    while True:
        answer = input(f"{label}: ").strip()
        if answer:
            return answer
        print("云端 provider 需要填写 API key。")


def _template_by_id(template_id: str | None) -> LLMTemplate | None:
    if not template_id:
        return None
    normalized = template_id.strip().lower()
    for template in TEMPLATES:
        if template.template_id == normalized:
            return template
    return None


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    """Parse one dotenv assignment line."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None
    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    return key, _parse_env_value(raw_value.strip())


def _parse_env_value(raw_value: str) -> str:
    if not raw_value:
        return ""
    try:
        parsed = shlex.split(f"VALUE={raw_value}", comments=True, posix=True)
    except ValueError:
        return raw_value
    if not parsed or "=" not in parsed[0]:
        return raw_value
    return parsed[0].split("=", 1)[1]


def _shell_quote(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,\-]+", value):
        return value
    return shlex.quote(value)


if __name__ == "__main__":
    raise SystemExit(main())
