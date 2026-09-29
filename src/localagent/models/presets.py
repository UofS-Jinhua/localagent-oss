"""Known endpoints, and model settings derived from what an endpoint reports.

Almost every service speaks the OpenAI chat-completions protocol (Anthropic and
Gemini included, through their compatibility endpoints), so a preset is only a
base URL, where the key comes from, and a sensible concurrency.
"""

from dataclasses import asdict, dataclass

from localagent.models.discovery import DiscoveredModel
from localagent.models.registry import ModelSpec, Provider

LMSTUDIO_THINKING_OFF = {"reasoning_effort": "none"}
FALLBACK_CONTEXT = 32768
MAX_DEFAULT_CLOUD_CONTEXT = 262144  # every step resends the conversation; a 1M window gets expensive


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    base_url: str
    api_key_env: str = ""
    max_concurrency: int = 2
    discovery: str = ""
    key_url: str = ""  # where to get a key
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


PRESETS = (
    Preset("lmstudio", "LM Studio", "http://localhost:1234/v1", discovery="lmstudio",
           note="Put the address of the computer running LM Studio, e.g. http://192.168.1.20:1234/v1."),
    Preset("ollama", "Ollama", "http://localhost:11434/v1", max_concurrency=1),
    Preset("openai_compatible", "Other OpenAI-compatible server (llama.cpp, vLLM, …)", "http://localhost:8080/v1"),
    Preset("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
           api_key_env="GEMINI_API_KEY", max_concurrency=1, discovery="gemini",
           key_url="https://aistudio.google.com/apikey"),
    Preset("openai", "OpenAI", "https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
           key_url="https://platform.openai.com/api-keys"),
    Preset("anthropic", "Anthropic (OpenAI-compatible endpoint)", "https://api.anthropic.com/v1",
           api_key_env="ANTHROPIC_API_KEY", key_url="https://console.anthropic.com/settings/keys",
           note="Uses Anthropic's OpenAI SDK compatibility layer: no prompt caching, reasoning is not streamed."),
    Preset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY",
           key_url="https://platform.deepseek.com/api_keys"),
    Preset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
           key_url="https://openrouter.ai/keys"),
    Preset("dashscope", "Qwen (Alibaba Cloud DashScope)", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           api_key_env="DASHSCOPE_API_KEY"),
)


def normalize_base_url(url: str) -> str:
    """Add ``/v1`` to a bare local server address; leave URLs that already name a path alone."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1") or url.count("/") > 2:
        return url
    return url + "/v1"


def chat_models(found: list[DiscoveredModel]) -> list[DiscoveredModel]:
    """The discovered models that can chat (no embedding, speech or image-generation models)."""
    skip = ("embed", "tts", "transcribe", "-image", "imagen", "veo", "lyria", "aqa", "banana")
    return [m for m in found if m.type in ("llm", "vlm") and not any(s in m.id.lower() for s in skip)]


def spec_from_discovery(found: DiscoveredModel, provider: Provider) -> ModelSpec:
    """Registry settings for a model an endpoint reported."""
    context = found.context_length or min(found.max_context_length or FALLBACK_CONTEXT, FALLBACK_CONTEXT)
    if not provider.is_local():
        context = min(context, MAX_DEFAULT_CLOUD_CONTEXT)
    thinking, thinking_off = "toggle", {}
    if provider.discovery == "lmstudio":
        thinking_off = dict(LMSTUDIO_THINKING_OFF)
    elif provider.discovery == "gemini":
        if found.thinking is False:
            thinking = "never"
        else:  # 2.5 Flash can switch thinking off; later models only turn it down
            thinking_off = {"reasoning_effort": "none" if "2.5-flash" in found.id else "low"}
    # What is loaded changes without us; it is read live (discovery.loaded_models) instead of
    # being written here, where it would go stale in models.yaml.
    notes = ", ".join(x for x in (found.arch, found.quantization) if x)
    spec = ModelSpec(
        name=found.id, provider=provider.name, model_id=found.id, context_length=context,
        thinking=thinking, thinking_off=thinking_off, notes=notes, vision=bool(found.vision),
    )
    if found.max_output_tokens:
        spec.max_output_tokens = min(spec.max_output_tokens, found.max_output_tokens)
    return spec
