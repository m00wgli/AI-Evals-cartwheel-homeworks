"""One place that decides where model calls go.

By default Cartwheel calls each course model on its own vendor API (OpenAI,
Anthropic through LiteLLM, Together AI through LiteLLM). Set the ``LLM_*``
variables in ``.env`` and every model call in the repository is routed
instead through a single OpenAI-compatible gateway, such as a company shared
LLM service:

    LLM_API_KEY=...                        # gateway key
    LLM_BASE_URL=https://.../v1            # OpenAI-compatible endpoint
    LLM_MODEL=openai.gpt-5.4-nano          # swap models by editing this line
    LLM_JUDGE_MODEL=                       # optional; defaults to LLM_MODEL

Both call paths in this repository are covered:

  - the Agents SDK (``agent/agent.py``): `configure` installs an
    ``AsyncOpenAI`` client pointed at the gateway as the SDK default and
    selects the Chat Completions API, which is what OpenAI-compatible
    gateways implement (the Responses API usually is not available). With
    that in place ``Agent(model="openai.gpt-5.4-nano")`` calls the gateway.
  - LiteLLM and DocETL (the frozen judges in ``replay/rollout.py``, the
    scaling backend in ``analysis/helpers/scale.py``): `litellm_kwargs`
    supplies ``api_base``/``api_key`` and `litellm_model_id` prefixes the
    model with ``openai/`` so LiteLLM treats it as a plain OpenAI-compatible
    endpoint. `configure` also mirrors the gateway into ``OPENAI_API_KEY``
    and ``OPENAI_BASE_URL`` for libraries such as DocETL that build their own
    client from the environment.

Nothing here reads ``.env`` itself. Entry points call
``observability.instrument.load_env()`` first, exactly as before, so tests
that stub that loader stay offline.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("cartwheel.llm")

# Course model names, resolved by the vendor-API path in agent/agent.py and
# pinned in frozen judges and eval configs. A gateway serves its own model ids
# instead, so these names are replaced by LLM_MODEL when the gateway is on.
COURSE_MODEL_NAMES = frozenset(
    {
        "gpt-5.5",
        "gpt-5.5-nano",
        "gpt-nano",
        "claude-opus-4-6",
        "glm-5.2",
        "gemini-flash",
        "gemini-flash-lite",
    }
)

_configured = False
_platform_key: str | None = None


def config() -> dict[str, str] | None:
    """The gateway settings, or None when the vendor APIs are in use.

    The gateway is on only when both the key and the base URL are set. A
    half-configured gateway raises instead of quietly falling back to a
    vendor API, which would be a different endpoint and a different bill.
    """
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not api_key and not base_url:
        return None
    if not api_key or not base_url:
        raise ValueError(
            "LLM_API_KEY and LLM_BASE_URL must be set together; set both to use "
            "the shared LLM service, or neither to call the vendor APIs"
        )
    return {"api_key": api_key, "base_url": base_url, "model": model}


def is_active() -> bool:
    """True when model calls go through the shared LLM service."""
    return config() is not None


def default_model() -> str:
    """The model used when no ``--model`` is given.

    LLM_MODEL wins while the gateway is on, because it is the one line the
    reader edits to swap models. CARTWHEEL_MODEL still selects among the
    course models on the vendor APIs.
    """
    from agent.agent import DEFAULT_MODEL

    gateway = config()
    if gateway and gateway["model"]:
        return gateway["model"]
    return os.environ.get("CARTWHEEL_MODEL") or DEFAULT_MODEL


def routes_to_gateway(name: str) -> bool:
    """Whether ``name`` should be called on the shared LLM service.

    A name with a slash is a LiteLLM provider route (``anthropic/...``,
    ``ollama_chat/local-model``): it names the provider to call, and the
    gateway is not it, so those keep going straight to that provider even
    while the gateway is on. Everything else routes through the gateway.
    """
    return is_active() and "/" not in name


def model_id(name: str, *, replacement: str | None = None) -> str:
    """Map a model name onto something the gateway serves.

    Course names (``gpt-5.5``, ``claude-opus-4-6``, ...) name vendor models
    the gateway does not host, so they become LLM_MODEL. Any other name
    passes through untouched, which is how an explicit gateway id such as
    ``openai.gpt-5.4-nano`` reaches it.
    """
    gateway = config()
    if not gateway or not routes_to_gateway(name):
        return name
    target = replacement or gateway["model"]
    if name in COURSE_MODEL_NAMES or _vendor_family(name):
        if not target:
            raise ValueError(
                f"model '{name}' names a vendor model; set LLM_MODEL to a model id "
                f"the shared LLM service at {gateway['base_url']} serves"
            )
        if name != target:
            log.warning("routing model '%s' to '%s' on the shared LLM service", name, target)
        return target
    return name


def judge_model(name: str) -> str:
    """The model id a frozen judge actually runs on.

    A Module 2 freeze pins a judge's prompt *and* its model id, and a judge
    scored by a different model is a different judge. The gateway does not
    host the course model names, so LLM_JUDGE_MODEL (or LLM_MODEL) stands in
    and `model_id` logs the substitution rather than hiding it.
    """
    gateway = config()
    if not gateway:
        return name
    replacement = os.environ.get("LLM_JUDGE_MODEL", "").strip() or gateway["model"]
    return model_id(name, replacement=replacement)


def _vendor_family(name: str) -> bool:
    """Names of vendor model families, which a gateway serves under its own ids."""
    return name.startswith(("gpt-", "claude", "glm", "gemini"))


def configure() -> bool:
    """Point the Agents SDK and the environment at the gateway. Idempotent.

    Returns True when the gateway is on. Safe to call from any entry point:
    with no ``LLM_*`` variables set it does nothing at all, so the course's
    vendor-API path is untouched.
    """
    global _configured
    gateway = config()
    if gateway is None or _configured:
        return gateway is not None

    from agents import set_default_openai_api, set_default_openai_client
    from openai import AsyncOpenAI

    global _platform_key

    client = AsyncOpenAI(api_key=gateway["api_key"], base_url=gateway["base_url"])
    # use_for_tracing=False: a gateway key is not an OpenAI platform key, so it
    # must never be used to upload traces to OpenAI.
    set_default_openai_client(client, use_for_tracing=False)
    # Gateways implement Chat Completions; the SDK would default to Responses.
    set_default_openai_api("chat_completions")
    # LiteLLM, DocETL and other libraries build their own OpenAI client from
    # the environment. Mirror the gateway so they reach it too. A real OpenAI
    # platform key already in the environment is kept for hosted tracing
    # (--trace-openai), the one thing the gateway key cannot do.
    prior = os.environ.get("OPENAI_API_KEY", "").strip()
    if prior and prior != gateway["api_key"]:
        from agents import set_tracing_export_api_key

        set_tracing_export_api_key(prior)
        _platform_key = prior
    os.environ["OPENAI_API_KEY"] = gateway["api_key"]
    os.environ["OPENAI_BASE_URL"] = gateway["base_url"]
    # A gateway names its models its own way, so LiteLLM cannot always tell
    # which OpenAI parameters a given model accepts (a gpt-5 family model, for
    # instance, rejects temperature). Dropping the unsupported ones keeps the
    # call alive; a judge then runs at the model's own default temperature.
    # LiteLLM reads this at import time, so set the attribute too when it is
    # already loaded (DocETL imports it eagerly).
    os.environ.setdefault("LITELLM_DROP_PARAMS", "1")
    litellm_module = sys.modules.get("litellm")
    if litellm_module is not None:
        litellm_module.drop_params = True
    log.info("model calls routed through the shared LLM service at %s", gateway["base_url"])
    _configured = True
    return True


def openai_platform_key() -> str | None:
    """A real OpenAI platform key, if one is configured.

    Hosted OpenAI tracing uploads with an OpenAI platform key, which the
    gateway key is not. This is how the ``--trace-openai`` check tells the two
    apart once `configure` has mirrored the gateway into OPENAI_API_KEY.
    """
    if _platform_key:
        return _platform_key
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    gateway = config()
    if gateway and key == gateway["api_key"]:
        return None
    return key or None


def litellm_model_id(name: str) -> str:
    """A LiteLLM model string for ``name``, honoring the gateway.

    ``openai/<id>`` is LiteLLM's route for any OpenAI-compatible endpoint;
    with `litellm_kwargs` it calls the gateway directly.
    """
    if not routes_to_gateway(name):
        return name
    resolved = model_id(name)
    return resolved if resolved.startswith("openai/") else f"openai/{resolved}"


def litellm_kwargs(model: str | None = None) -> dict[str, Any]:
    """``api_base``/``api_key`` for a LiteLLM call; ``{}`` off the gateway.

    Pass the model so a provider route that bypasses the gateway does not get
    the gateway's endpoint attached to it.
    """
    gateway = config()
    if gateway is None or (model is not None and not routes_to_gateway(model)):
        return {}
    return {
        "api_base": gateway["base_url"],
        "api_key": gateway["api_key"],
        # See configure(): the gateway's model ids are not LiteLLM's, so let it
        # drop parameters the target model does not accept instead of failing.
        "drop_params": True,
    }


def provider_key_name(model: str) -> str | None:
    """The environment variable that pays for ``model``.

    One gateway key covers every model while the gateway is on, so the
    per-vendor key checks elsewhere in the repo collapse to LLM_API_KEY.
    """
    if is_active():
        return "LLM_API_KEY"
    if model.startswith("gpt") or model.startswith("openai/"):
        return "OPENAI_API_KEY"
    if model.startswith("claude") or model.startswith("anthropic/"):
        return "ANTHROPIC_API_KEY"
    if model.startswith("glm") or model.startswith("together_ai/") or model.startswith("zai-org/"):
        return "TOGETHER_API_KEY"
    return None


def have_model_key() -> bool:
    """True when some credential for a live model call is configured."""
    return any(
        os.environ.get(name, "").strip()
        for name in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TOGETHER_API_KEY")
    )
