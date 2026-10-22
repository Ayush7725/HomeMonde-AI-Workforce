"""Model provider adapters behind one interface.

Messages are kept in one neutral shape (Anthropic-style blocks):
  user:      str | [{"type": "tool_result", "tool_use_id", "content", "is_error"?}]
  assistant: [{"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}]
Each adapter converts to and from its API and returns a Reply. Errors map to the platform's three outcomes
(retry later, unavailable, refused), so budgets, evidence checks and audit behave the same on every provider."""
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import anthropic

from .tenancy import ProviderConfig, resolve_env

# Built-in providers. Gemini, DeepSeek and Hugging Face are reached through their OpenAI-compatible endpoints.
BUILTIN = {
    "anthropic": ProviderConfig(kind="anthropic", api_key_env="ANTHROPIC_API_KEY"),
    "openai": ProviderConfig(base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY", structured="json_schema",
                             max_tokens_param="max_completion_tokens"),
    "gemini": ProviderConfig(base_url="https://generativelanguage.googleapis.com/v1beta/openai", api_key_env="GEMINI_API_KEY",
                             structured="json_schema"),
    "deepseek": ProviderConfig(base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY", structured="json_object"),
    "huggingface": ProviderConfig(base_url="https://router.huggingface.co/v1", api_key_env="HF_TOKEN", structured="json_object"),
}
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
NO_EFFORT_MODELS = ("claude-haiku-4", "claude-3")  # these reject output_config.effort


class Retryable(Exception):
    """A transient failure. retry_after: seconds the provider asked us to wait (a rate limit), when it said."""

    def __init__(self, msg: str = "", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _wait_seconds(headers, body: str = "") -> float | None:
    """How long a provider asked us to wait: a Retry-After header, or a retryDelay ('7706s') in the error body."""
    v = headers.get("retry-after") if headers is not None else None
    if v:
        try:
            return max(0.0, float(v))
        except ValueError:
            pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body or "")
    return float(m.group(1)) if m else None


class Unavailable(Exception):
    pass


class Refused(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Reply:
    blocks: list[dict]
    stop: str  # tool_use | end
    model: str
    usage: Usage = field(default_factory=Usage)
    raw: object = None  # provider content replayed unchanged on the same model (keeps Claude thinking blocks valid)

    @property
    def text(self) -> str:
        return "".join(b["text"] for b in self.blocks if b["type"] == "text")

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.blocks if b["type"] == "tool_use"]


def schema_instruction(schema: dict) -> str:
    return ("\nReturn the final answer as one JSON object (no prose, no code fences) matching this JSON schema:\n"
            + json.dumps(schema, separators=(",", ":")))


# ---------------------------------------------------------------- Anthropic

def anthropic_client(api_key: str | None = None):
    return anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=120.0) if api_key else anthropic.Anthropic(max_retries=2, timeout=120.0)


class AnthropicAdapter:
    client_factory = staticmethod(anthropic_client)  # replaced in tests

    def __init__(self, name: str, conf: ProviderConfig, api_key: str | None = None):
        self.name, self.conf, self._client, self.api_key = name, conf, None, api_key

    def has_credentials(self) -> bool:
        if AnthropicAdapter.client_factory is not anthropic_client or self.api_key:
            return True
        return bool(os.environ.get(self.conf.api_key_env or "ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def _messages(self, messages: list, model: str) -> list:
        out = []
        for m in messages:
            raw = m.get("_raw")
            if m["role"] == "assistant" and raw and raw[0] == model:
                out.append({"role": "assistant", "content": raw[1]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def complete(self, *, model, system, messages, tools, schema, effort, max_tokens, final, fallback) -> Reply:
        if self._client is None:
            factory = AnthropicAdapter.client_factory
            self._client = factory(self.api_key) if factory is anthropic_client else factory()
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if not model.startswith(NO_EFFORT_MODELS):
            output_config["effort"] = effort
        kwargs = dict(model=model, max_tokens=max_tokens, system=[{"type": "text", "text": system}],
                      messages=self._messages(messages, model), cache_control={"type": "ephemeral"}, output_config=output_config)
        if tools:
            kwargs["tools"] = tools
            if final:
                kwargs["tool_choice"] = {"type": "none"}
        if fallback and model in CLAUDE_FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        try:
            resp = self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise Retryable("rate limited", _wait_seconds(getattr(e.response, "headers", None))) from e
        except anthropic.APIConnectionError as e:
            raise Retryable("connection error") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409, 529):
                raise Retryable(f"API error {e.status_code}") from e
            raise Unavailable(f"AI request rejected ({e.status_code})") from e
        u = resp.usage
        usage = Usage(u.input_tokens or 0, u.output_tokens or 0, getattr(u, "cache_read_input_tokens", 0) or 0,
                      getattr(u, "cache_creation_input_tokens", 0) or 0)
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input or {}})
        reply = Reply(blocks, "tool_use" if resp.stop_reason == "tool_use" else "end", resp.model or model, usage, (model, resp.content))
        if resp.stop_reason == "refusal":
            raise _with_usage(Refused("the AI model declined this request"), reply)
        if resp.stop_reason == "max_tokens":
            raise _with_usage(Retryable("response exceeded max_tokens"), reply)
        return reply


# ---------------------------------------------------------------- OpenAI-compatible (OpenAI, Gemini, DeepSeek, Hugging Face, others)

def http_post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    """POST JSON and return JSON. Replaced in tests. Raises urllib errors for the adapter to classify."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL comes from deployment config
        return json.loads(resp.read())


_default_http_post = http_post


class OpenAICompatAdapter:
    post = staticmethod(http_post)  # replaced in tests

    def __init__(self, name: str, conf: ProviderConfig, api_key: str | None = None):
        self.name, self.conf, self.api_key = name, conf, api_key

    def has_credentials(self) -> bool:
        if OpenAICompatAdapter.post is not _default_http_post or self.api_key:
            return True
        return not self.conf.api_key_env or bool(os.environ.get(self.conf.api_key_env))

    def _messages(self, system: str, messages: list, model: str | None = None) -> list:
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if m["role"] == "user":
                if isinstance(c, str):
                    out.append({"role": "user", "content": c})
                else:
                    for r in c:
                        out.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
            else:
                text = "".join(b["text"] for b in c if b["type"] == "text")
                raw = m.get("_raw")
                # provider data attached to a tool call (Gemini's thought_signature) goes back to the same model unchanged
                extras = raw[1] if raw and raw[0] == model and isinstance(raw[1], dict) else {}
                calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                          **({"extra_content": extras[b["id"]]} if b["id"] in extras else {})}
                         for b in c if b["type"] == "tool_use"]
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
        return out

    def complete(self, *, model, system, messages, tools, schema, effort, max_tokens, final, fallback) -> Reply:
        conf = self.conf
        if conf.structured != "json_schema":
            system = system + schema_instruction(schema)
        body = {"model": model, "messages": self._messages(system, messages, model), conf.max_tokens_param: max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                "parameters": t["input_schema"]}} for t in tools]
            body["tool_choice"] = "none" if final else "auto"
        if conf.structured == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        elif conf.structured == "json_object":
            body["response_format"] = {"type": "json_object"}
        if conf.reasoning_effort:
            body["reasoning_effort"] = {"xhigh": "high", "max": "high"}.get(effort, effort)
        headers = {}
        key = self.api_key or (os.environ.get(conf.api_key_env) if conf.api_key_env else None)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = resolve_env(conf.base_url).rstrip("/") + "/chat/completions"
        try:
            data = OpenAICompatAdapter.post(url, headers, body, conf.timeout_seconds)
        except urllib.error.HTTPError as e:
            body = _body(e)
            if e.code in (408, 409, 429) or e.code >= 500:
                raise Retryable(f"{self.name} API error {e.code}", _wait_seconds(e.headers, body)) from None
            raise Unavailable(f"{self.name} rejected the request ({e.code}){_reason(body)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Retryable(f"{self.name} unreachable ({type(e).__name__})") from None
        except ValueError:
            raise Retryable(f"{self.name} returned an unreadable response") from None
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            raise Retryable(f"{self.name} returned no choices") from None
        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or u.get("prompt_cache_hit_tokens") or 0
        usage = Usage(max(0, (u.get("prompt_tokens") or 0) - cached), u.get("completion_tokens") or 0, cached, 0)
        blocks, extras = [], {}
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"__unparseable_arguments__": (fn.get("arguments") or "")[:200]}
            blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{len(blocks)}", "name": fn.get("name", ""),
                           "input": args if isinstance(args, dict) else {}})
            if call.get("extra_content"):
                extras[blocks[-1]["id"]] = call["extra_content"]
        reply = Reply(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end", data.get("model") or model, usage,
                      raw=(model, extras) if extras else None)
        reason = choice.get("finish_reason")
        if msg.get("refusal") or reason == "content_filter":
            raise _with_usage(Refused("the AI model declined this request"), reply)
        if reason == "length":
            raise _with_usage(Retryable("response exceeded max tokens"), reply)
        return reply


def _body(e: urllib.error.HTTPError) -> str:
    try:
        return (e.read() or b"").decode("utf-8", "replace")[:20000]
    except (OSError, AttributeError, ValueError):  # a bare HTTPError may carry no body at all
        return ""


def _reason(text: str) -> str:
    """The provider's own explanation of a refused request (e.g. a missing field), so the log says what to fix."""
    try:
        body = json.loads(text or "null")
        err = (body[0] if isinstance(body, list) and body else body or {}).get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError, OSError):
        return ""
    return f": {' '.join(str(msg).split())[:300]}" if msg else ""


def _with_usage(exc: Exception, reply: Reply) -> Exception:
    exc.reply = reply  # tokens are still billed and recorded
    return exc


def adapter(name: str, custom: dict[str, ProviderConfig], api_key: str | None = None):
    """api_key: a key saved in the panel; without one the adapter reads the provider's environment variable."""
    conf = custom.get(name) or BUILTIN[name]
    return (AnthropicAdapter if conf.kind == "anthropic" else OpenAICompatAdapter)(name, conf, api_key)
