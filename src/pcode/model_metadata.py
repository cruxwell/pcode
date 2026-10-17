"""Context, output and input-kind metadata, not pricing. Refreshes never run during rendering.

Public Models.dev metadata is cached on disk. Authenticated metadata is cached
per model instance only: one account/proxy must not inherit another's limits.
Failures retain last-known values; unknown serving routes stay unknown.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import quote, urlsplit
from weakref import finalize

import httpx2
from pydantic_ai.models import Model

CATALOG_URL = "https://models.dev/api.json"
CATALOG_TTL = 24 * 60 * 60
NATIVE_TTL = 60 * 60
RETRY_DELAY = 60
REQUEST_TIMEOUT = 3
# Protocol version verified against OpenAI's models endpoint implementation.
# This is NOT the pcode version; the backend uses it to select its catalog schema.
CODEX_CATALOG_VERSION = "0.154.0"


def codex_catalog_version() -> str:
    """Use the newest verified/catalog client version, not pcode's own version.

    The backend filters out newer models for older clients. The local Codex cache
    is useful for its protocol version only: never borrow its account's limits,
    instructions, credentials, or model availability. No CLI subprocess needed.
    """
    version = CODEX_CATALOG_VERSION
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        with (home / "models_cache.json").open() as file:
            raw = file.read(8_000_001)
        if len(raw) > 8_000_000:
            return version
        data = json.loads(raw)
        candidate = data.get("client_version") if isinstance(data, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"\d{1,3}\.\d{1,4}\.\d{1,4}", candidate):
            if tuple(map(int, candidate.split("."))) > tuple(map(int, version.split("."))):
                version = candidate
    except OSError, ValueError:
        pass
    return version


class ContextWindowError(ValueError):
    """Invalid explicit working-window configuration (safe to display)."""


def positive_int(value) -> int | None:
    return value if type(value) is int and 0 < value <= 100_000_000 else None


@dataclass(frozen=True)
class ModelLimits:
    context: int | None = None
    input: int | None = None
    output: int | None = None
    maximum: int | None = None
    source: str = ""
    fetched_at: float = 0
    # Kinds of input the model takes ("text", "image", "pdf"), as its source
    # lists them. Empty means the source said nothing, which is not text only.
    inputs: tuple[str, ...] = ()

    def working_window(self, override: int | None = None) -> int | None:
        # Input ceilings are distinct from the combined input+output context.
        # Codex's default window may be smaller than its opt-in maximum.
        if override is not None:
            limits = [override, self.maximum or self.context, self.input]
        else:
            limits = [self.context, self.input]
        known = [limit for limit in limits if limit is not None]
        return min(known) if known else None


def configured_window() -> int | None:
    value = os.environ.get("PCODE_CONTEXT_WINDOW", "").strip()
    if not value:
        return None
    try:
        window = int(value)
    except ValueError:
        raise ContextWindowError("PCODE_CONTEXT_WINDOW must be a positive token count.") from None
    if window < 4096:
        raise ContextWindowError("PCODE_CONTEXT_WINDOW must be at least 4096 tokens.")
    return window


def parse_inputs(value) -> tuple[str, ...]:
    """A source's list of input kinds; anything else is unknown, not empty."""
    if not isinstance(value, list):
        return ()
    return tuple(kind for kind in value if isinstance(kind, str))


def parse_catalog(data, fetched_at: float) -> dict[str, ModelLimits]:
    result = {}
    if not isinstance(data, dict):
        return result
    for provider, entry in data.items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            continue
        models = entry.get("models")
        if not isinstance(models, dict):
            continue
        for name, model in models.items():
            if not isinstance(name, str) or not isinstance(model, dict):
                continue
            limits = model.get("limit")
            if not isinstance(limits, dict):
                continue
            context = positive_int(limits.get("context"))
            input_limit = positive_int(limits.get("input"))
            if context or input_limit:
                modalities = model.get("modalities")
                result[f"{provider}:{name}"] = ModelLimits(
                    context=context,
                    input=input_limit,
                    output=positive_int(limits.get("output")),
                    source="models.dev",
                    fetched_at=fetched_at,
                    inputs=parse_inputs(
                        modalities.get("input") if isinstance(modalities, dict) else None
                    ),
                )
    return result


def parse_codex(data, name: str, fetched_at: float) -> ModelLimits | None:
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    for model in models:
        if not isinstance(model, dict) or model.get("slug") != name:
            continue
        context = positive_int(model.get("context_window"))
        maximum = positive_int(model.get("max_context_window"))
        if context:
            return ModelLimits(
                context=context,
                maximum=max(context, maximum or context),
                output=positive_int(model.get("max_output_tokens")),
                source="codex",
                fetched_at=fetched_at,
                inputs=parse_inputs(model.get("input_modalities")),
            )
    return None


def parse_anthropic(data, fetched_at: float) -> ModelLimits | None:
    if not isinstance(data, dict):
        return None
    input_limit = positive_int(data.get("max_input_tokens"))
    output_limit = positive_int(data.get("max_tokens"))
    capabilities = data.get("capabilities")
    images = capabilities.get("image_input") if isinstance(capabilities, dict) else None
    supported = images.get("supported") if isinstance(images, dict) else None
    if input_limit or output_limit:
        return ModelLimits(
            input=input_limit,
            output=output_limit,
            source="anthropic",
            fetched_at=fetched_at,
            inputs=("text", "image")
            if supported is True
            else ("text",)
            if supported is False
            else (),
        )
    return None


def catalog_routes(data) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    return {
        name: entry["api"]
        for name, entry in data.items()
        if isinstance(name, str)
        and isinstance(entry, dict)
        and isinstance(entry.get("api"), str)
        and entry["api"].startswith("https://")
    }


def catalog_key(model: str | Model, routes: dict[str, str] | None = None) -> str | None:
    """Exact provider/model match, never guess from a model's family name."""
    identity = model if isinstance(model, str) else model.model_id
    provider, sep, name = identity.partition(":")
    if not sep:
        return None
    if provider == "claude":
        # Claude Code serves Anthropic's models under their own names.
        return f"anthropic:{name}"
    if not isinstance(model, str):
        endpoint = model.provider
        if endpoint is None:
            return None
        # Some catalogs omit `api` for native SDK providers. These are verified
        # canonical endpoints, not model limits. Other routes come from the feed.
        canonical = {
            "anthropic": "https://api.anthropic.com",
            "openai": "https://api.openai.com/v1",
            "openai-codex": "https://chatgpt.com/backend-api/codex",
        }.get(endpoint.name) or (routes or {}).get(endpoint.name)
        if canonical is None:
            return None
        # Use the client's actual URL, not an SDK default that may hide an
        # injected client/proxy. Require scheme, port and path to match too.
        actual = str(getattr(endpoint.client, "base_url", ""))
        try:
            url, expected = urlsplit(actual), urlsplit(canonical)
            if (
                (url.scheme, url.netloc, url.path.rstrip("/"))
                != (expected.scheme, expected.netloc, expected.path.rstrip("/"))
                or url.query
                or url.fragment
            ):
                return None
        except ValueError:
            return None
        provider = endpoint.name
    provider = {"openai-responses": "openai", "openai-chat": "openai"}.get(provider, provider)
    # In particular, openai-codex NEVER maps to openai.
    return f"{provider}:{name}"


@dataclass
class NativeState:
    limits: ModelLimits | None = None
    attempted_at: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ContextCatalog:
    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path
        self.public: dict[str, ModelLimits] = {}
        self.routes: dict[str, str] = {}
        self._states: dict[int, NativeState] = {}
        self._public_lock = asyncio.Lock()
        self._public_attempt = 0.0
        self._public_updated = 0.0
        self._loaded = False

    def state(self, model: Model) -> NativeState:
        # Pydantic models are unhashable. Key by identity with weak cleanup, not
        # by model name: credentials and endpoints belong to this exact instance.
        key = id(model)
        if key not in self._states:
            self._states[key] = NativeState()
            finalize(model, self._states.pop, key, None)
        return self._states[key]

    def _path(self) -> Path:
        if self.cache_path is not None:
            return self.cache_path
        root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        return root / "pcode" / "model-context-v1.json"

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text())
            version = data.get("version")
            if version not in (1, 2):
                return
            updated = data["fetched_at"]
            if type(updated) not in (int, float) or not 0 < updated <= time.time():
                return
            entries = data["models"]
            if not isinstance(entries, dict):
                return
            parsed = {}
            for key, row in entries.items():
                if not isinstance(key, str) or not isinstance(row, dict):
                    continue
                limits = ModelLimits(
                    context=positive_int(row.get("context")),
                    input=positive_int(row.get("input")),
                    output=positive_int(row.get("output")),
                    source="models.dev",
                    fetched_at=updated,
                    inputs=parse_inputs(row.get("inputs")),
                )
                if limits.working_window():
                    parsed[key] = limits
            if parsed:
                self.public = parsed
                routes = data.get("routes", {})
                if isinstance(routes, dict):
                    self.routes = {
                        key: value
                        for key, value in routes.items()
                        if isinstance(key, str)
                        and isinstance(value, str)
                        and value.startswith("https://")
                    }
                # A version 1 cache predates input kinds: good enough offline,
                # but due for a refresh now rather than in a day.
                self._public_updated = updated if version == 2 else 0
        except OSError, ValueError, TypeError, KeyError, AttributeError:
            pass

    def _save(self) -> None:
        path = self._path()
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(
                    {
                        "version": 2,
                        "fetched_at": self._public_updated,
                        "models": {key: asdict(value) for key, value in self.public.items()},
                        "routes": self.routes,
                    },
                    file,
                )
            temporary.replace(path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def limits(self, model: str | Model) -> ModelLimits | None:
        if not isinstance(model, str) and (native := self.state(model).limits) is not None:
            return native
        key = catalog_key(model, self.routes)
        return self.public.get(key) if key else None

    def window(self, model: str | Model) -> int | None:
        override = configured_window()
        limits = self.limits(model)
        return limits.working_window(override) if limits else override

    def inputs(self, model: str | Model) -> tuple[str, ...]:
        """Kinds of input `model` takes, or empty when no source says.

        The account's own answer wins, but one that is silent on input must not
        hide the public catalog's: Anthropic's endpoint predates the field.
        """
        if not isinstance(model, str) and (native := self.state(model).limits) and native.inputs:
            return native.inputs
        key = catalog_key(model, self.routes)
        entry = self.public.get(key) if key else None
        return entry.inputs if entry else ()

    async def refresh_public(self) -> None:
        async with self._public_lock:
            if not self._loaded:
                await asyncio.to_thread(self._load)
                self._loaded = True
            now = time.time()
            if now - self._public_updated < CATALOG_TTL:
                return
            if now - self._public_attempt < RETRY_DELAY:
                return
            self._public_attempt = now
            try:
                # Dedicated anonymous client: never send provider auth to models.dev.
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                        response = await client.get(CATALOG_URL)
                        response.raise_for_status()
                        data = response.json()
                        parsed = parse_catalog(data, now)
                if parsed:
                    self.public = parsed
                    self.routes = catalog_routes(data)
                    self._public_updated = now
                    await asyncio.to_thread(self._save)
            except Exception:
                # Metadata is optional. Do not expose bodies, credentials, or login
                # failures in the UI; the actual model request owns auth errors.
                pass

    async def refresh_native(self, model: Model) -> None:
        provider = model.provider
        if provider is None or provider.name not in {"openai-codex", "anthropic"}:
            return
        state = self.state(model)
        async with state.lock:
            now = time.time()
            previous = self.state(model).limits
            if previous and now - previous.fetched_at < NATIVE_TTL:
                return
            if now - state.attempted_at < RETRY_DELAY:
                return
            state.attempted_at = now
            try:
                # The SDK client carries the real base URL, proxy, credential
                # rotation and 401 replay. No parallel auth implementation here.
                async with asyncio.timeout(REQUEST_TIMEOUT), model:
                    client = provider.client
                    options = {"max_retries": 0, "timeout": REQUEST_TIMEOUT}
                    if provider.name == "openai-codex":
                        version = await asyncio.to_thread(codex_catalog_version)
                        data = await client.get(
                            "/models",
                            cast_to=dict[str, Any],
                            options={
                                **options,
                                "params": {"client_version": version},
                            },
                        )
                        limits = parse_codex(data, model.model_name, now)
                    else:
                        # Raw JSON works with installed SDKs predating these fields.
                        data = await client.get(
                            f"/v1/models/{quote(model.model_name, safe='')}",
                            cast_to=dict[str, Any],
                            options=options,
                        )
                        limits = parse_anthropic(data, now)
                if limits is not None:
                    state.limits = limits
            except Exception:
                pass

    async def refresh(self, model: str | Model | None) -> None:
        if model is None:
            return
        if isinstance(model, str):
            # Native state belongs to a retained model instance, never a
            # temporary inferred model or a shared provider:name auth cache.
            await self.refresh_public()
            return
        if model.provider is None:
            return
        await asyncio.gather(self.refresh_public(), self.refresh_native(model))


catalog = ContextCatalog()


def context_window(model: str | Model) -> int | None:
    """Synchronous, memory-only lookup shared by the UI and compaction."""
    return catalog.window(model)


def model_inputs(model: str | Model) -> tuple[str, ...]:
    """Kinds of input the model takes, as far as any source says; memory-only like the window."""
    return catalog.inputs(model)


async def refresh_context(model: str | Model | None) -> None:
    await catalog.refresh(model)
