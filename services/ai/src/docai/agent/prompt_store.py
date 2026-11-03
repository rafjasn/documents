"""The chat graph's prompts: their text in code, their versions in Langfuse (Stage 10a).

The repository holds each prompt's text (`docai.agent.prompts`), reviewed like code.
`docai prompts push` makes a Langfuse version of each prompt whose text changed and
labels it `candidate`; `docai prompts promote --evaluation` moves `production` to the
versions a passed CI evaluation ran on, and `docai prompts rollback` moves it back.

The ai-service serves the versions under its label (LANGFUSE_PROMPT_LABEL, default
`production`) through the Langfuse SDK's cache, so a promotion needs no redeploy. The
cache keeps a version for 60 s, then serves it once more while it refreshes, so a
promotion reaches the second request after that.
Without Langfuse, when it is unreachable, or when a prompt has no version under the
label, the code's text serves, so Langfuse stays optional. Evaluations use the code's
prompts unless given a label (`--prompts`), and then pin the label's versions when they
start (`PromptStore.pinned`): there is no fallback, every prompt must have a version,
and the run records each version with the hash of its text. A label moved during or
after the evaluation changes neither what it ran on nor what a promotion moves.

Variables use Langfuse's `{{name}}` syntax in both places, rendered by one function, so
the same text gives the same prompt wherever it comes from. The structured-output
schemas and the Stage 06 unguarded prompts stay in code.
"""

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from docai.agent import prompts

logger = logging.getLogger(__name__)

# Langfuse prompt name -> the code's text
CODE: dict[str, str] = {
    "chat-route": prompts.ROUTE,
    "chat-plan": prompts.PLAN,
    "chat-grade": prompts.GRADE,
    "chat-rewrite": prompts.REWRITE,
    "chat-answer": prompts.ANSWER,
    "chat-grounding": prompts.GROUNDING,
    "chat-smalltalk": prompts.SMALLTALK,
}
CACHE_SECONDS = 60
FETCH_SECONDS = (
    2  # a slow or unreachable Langfuse costs a request at most this, per prompt
)
VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, **variables: object) -> str:
    """The template with its `{{name}}` variables filled; a missing one is an error."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise KeyError(f"no value for {{{{{name}}}}}")
        return str(variables[name])

    return VARIABLE.sub(value, template)


@dataclass(frozen=True)
class Rendered:
    """A prompt ready to send, and where it came from."""

    name: str
    text: str
    version: int | None = None  # None: the code's text
    source: Any = None  # the Langfuse prompt, which links the generation to its version

    @property
    def label(self) -> str:
        return f"{self.name}@{'code' if self.version is None else f'v{self.version}'}"


def code_prompt(name: str, template: str, **variables: object) -> Rendered:
    return Rendered(name, render(template, **variables))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PromptStore:
    """Prompts for the graph, from Langfuse under `label` or from the code.

    `client` is the Langfuse client of `docai.core.tracing` (None with tracing off), and
    `label` None means the code's prompts. `pinned` (see `PromptStore.pinned`) serves
    fixed versions instead of the label's current ones.
    """

    def __init__(
        self,
        client: Any = None,
        label: str | None = None,
        pinned: dict[str, Any] | None = None,
    ) -> None:
        self.client = client if label else None
        self.label = label
        self._pinned = pinned or {}
        self._unavailable: dict[str, float] = {}  # name -> retry after (monotonic)
        self._lock = threading.Lock()

    @classmethod
    def pinned(cls, client: Any, label: str) -> "PromptStore":
        """The versions under `label` now, served for the store's life whatever the
        label points at later. Raises if a prompt has none or Langfuse is unreachable:
        an evaluation of `candidate` must not quietly evaluate the code's text."""
        return cls(client, label, resolve(client, label))

    @property
    def remote(self) -> bool:
        return self.client is not None

    def _fetch(self, name: str) -> Any | None:
        if self._pinned:
            return self._pinned[name]
        if self.client is None:
            return None
        with self._lock:
            if self._unavailable.get(name, 0.0) > time.monotonic():
                return None
        try:
            prompt = self.client.get_prompt(
                name,
                label=self.label,
                type="text",
                cache_ttl_seconds=CACHE_SECONDS,
                fallback=CODE[name],
                max_retries=0,
                fetch_timeout_seconds=FETCH_SECONDS,
            )
        except Exception:
            logger.warning(
                "Prompt %s unavailable; using the code's", name, exc_info=True
            )
            prompt = None
        if prompt is None or getattr(prompt, "is_fallback", False):
            # Not asked again until the cache period passes, so a missing label or an
            # outage doesn't cost every request a round trip.
            with self._lock:
                self._unavailable[name] = time.monotonic() + CACHE_SECONDS
            return None
        return prompt

    def render(self, name: str, **variables: object) -> Rendered:
        prompt = self._fetch(name)
        if prompt is None:
            return code_prompt(name, CODE[name], **variables)
        return Rendered(
            name, render(prompt.prompt, **variables), prompt.version, prompt
        )

    def versions(self) -> dict[str, int | None]:
        """The version each prompt would serve now; None for the code's text."""
        return {
            name: getattr(self._fetch(name), "version", None) for name in sorted(CODE)
        }

    def describe(self) -> dict[str, Any]:
        """For run records: the label asked for and the version of each prompt, and for
        pinned versions the hash of each text, which a promotion checks."""
        described: dict[str, Any] = {
            "label": self.label or "code",
            "versions": self.versions(),
        }
        if self._pinned:
            described["sha256"] = {
                name: text_hash(self._pinned[name].prompt) for name in sorted(CODE)
            }
        return described

    def warm(self) -> None:
        """Fetch every prompt once, so the first request doesn't wait for Langfuse."""
        for name in CODE:
            self._fetch(name)


# Managing versions and labels (`docai prompts`) -------------------------------------

CANDIDATE, PRODUCTION, PREVIOUS = "candidate", "production", "previous"


def fetch(client: Any, name: str, label: str) -> Any | None:
    """The version of `name` under `label`, fresh from Langfuse; None if there is none.
    Other failures, such as an unreachable Langfuse, raise."""
    try:
        return client.get_prompt(
            name, label=label, type="text", cache_ttl_seconds=0, max_retries=1
        )
    except Exception as error:
        if getattr(error, "status_code", None) == 404:
            return None
        raise


def resolve(client: Any, label: str) -> dict[str, Any]:
    """Every prompt's version under `label`, fresh from Langfuse. A prompt without one
    is an error, and so (from `fetch`) is an unreachable Langfuse."""
    found, missing = {}, []
    for name in CODE:
        prompt = fetch(client, name, label)
        if prompt is None or getattr(prompt, "is_fallback", False):
            missing.append(name)
        else:
            found[name] = prompt
    if missing:
        raise LookupError(
            f"no {label} version of {', '.join(missing)}; run `docai prompts push`"
        )
    return found


def labels_with(prompt: Any, *added: str) -> list[str]:
    """The version's labels plus `added`. Langfuse keeps each label on one version, so
    giving a version a label takes it off the version that had it."""
    kept = [
        label for label in (getattr(prompt, "labels", None) or []) if label != "latest"
    ]
    return sorted({*kept, *added})


def matching_version(client: Any, name: str, text: str) -> Any | None:
    """The newest version of `name` whose text is `text`, if any: after a revert, the
    code matches an older version, which is reused rather than duplicated."""
    latest = fetch(client, name, "latest")
    if latest is None:
        return None
    for version in range(latest.version, 0, -1):
        prompt = (
            latest
            if version == latest.version
            else fetch_version(client, name, version)
        )
        if prompt is not None and prompt.prompt == text:
            return prompt
    return None


def fetch_version(client: Any, name: str, version: int) -> Any | None:
    try:
        return client.get_prompt(
            name, version=version, type="text", cache_ttl_seconds=0, max_retries=1
        )
    except Exception as error:
        if getattr(error, "status_code", None) == 404:
            return None
        raise


def push(client: Any, message: str) -> list[str]:
    """Label `candidate` the version matching each prompt's code, creating it if the
    text is new. Every prompt gets a candidate, so a promotion moves all of them to
    what the repository holds."""
    lines = []
    for name, text in CODE.items():
        existing = matching_version(client, name, text)
        if existing is not None:
            if CANDIDATE in (existing.labels or []):
                lines.append(
                    f"{name}: v{existing.version} unchanged, already the candidate"
                )
                continue
            client.update_prompt(
                name=name,
                version=existing.version,
                new_labels=labels_with(existing, CANDIDATE),
            )
            lines.append(
                f"{name}: v{existing.version} matches the code, labelled candidate"
            )
            continue
        created = client.create_prompt(
            name=name,
            prompt=text,
            labels=[CANDIDATE],
            type="text",
            commit_message=message,
        )
        lines.append(f"{name}: v{created.version} created, labelled candidate")
    return lines


def production_versions(client: Any) -> dict[str, int | None]:
    """The version each prompt has under `production` now; None without one."""
    return {
        name: getattr(fetch(client, name, PRODUCTION), "version", None)
        for name in sorted(CODE)
    }


def evaluated_prompts(record: dict[str, Any]) -> dict[str, Any]:
    """The prompts a passed CI evaluation ran on, from its record (`docai evals ci`)."""
    summary = record.get("summary") or {}
    if record.get("kind") != "ci" or summary.get("passed") is not True:
        raise ValueError("not a passed CI evaluation; nothing to promote")
    prompts: dict[str, Any] = summary.get("prompts") or {}
    return prompts


def promote(client: Any, evaluated: dict[str, Any]) -> list[str]:
    """Move `production` to the versions an evaluation ran on, whatever `candidate`
    points at now. `evaluated` is the evaluation's `PromptStore.describe()`: each
    prompt's version and text hash. Every version is checked against its hash before
    any label moves. The version `production` leaves becomes `previous`, for a
    rollback."""
    versions, hashes = evaluated.get("versions") or {}, evaluated.get("sha256") or {}
    if any(versions.get(name) is None or name not in hashes for name in CODE):
        raise ValueError(
            "the evaluation didn't run on pinned Langfuse versions of every prompt"
        )
    chosen = {}
    for name in CODE:
        prompt = fetch_version(client, name, versions[name])
        if prompt is None or text_hash(prompt.prompt) != hashes[name]:
            raise ValueError(f"{name} v{versions[name]} isn't the text evaluated")
        chosen[name] = prompt
    lines = []
    for name, prompt in chosen.items():
        candidate = fetch(client, name, CANDIDATE)
        moved = (
            f" (candidate is now v{candidate.version}, not evaluated)"
            if candidate is not None and candidate.version != prompt.version
            else ""
        )
        production = fetch(client, name, PRODUCTION)
        if production is not None and production.version == prompt.version:
            lines.append(f"{name}: v{prompt.version} already in production{moved}")
            continue
        if production is not None:
            client.update_prompt(
                name=name,
                version=production.version,
                new_labels=labels_with(production, PREVIOUS),
            )
        client.update_prompt(
            name=name,
            version=prompt.version,
            new_labels=labels_with(prompt, PRODUCTION),
        )
        before = f"v{production.version}" if production is not None else "none"
        lines.append(f"{name}: production {before} -> v{prompt.version}{moved}")
    return lines


def rollback(client: Any) -> list[str]:
    """Move `production` back to the version labelled `previous`."""
    lines = []
    for name in CODE:
        previous = fetch(client, name, PREVIOUS)
        production = fetch(client, name, PRODUCTION)
        if previous is None or (
            production is not None and production.version == previous.version
        ):
            lines.append(f"{name}: nothing to roll back")
            continue
        client.update_prompt(
            name=name,
            version=previous.version,
            new_labels=labels_with(previous, PRODUCTION),
        )
        before = f"v{production.version}" if production is not None else "none"
        lines.append(f"{name}: production {before} -> v{previous.version}")
    return lines


def status(client: Any) -> tuple[list[str], bool]:
    """Where each label stands against the repository's text, and whether `production`
    serves exactly what the repository holds."""
    lines, in_sync = [], True
    for name, text in CODE.items():
        parts = []
        for label in (PRODUCTION, CANDIDATE):
            prompt = fetch(client, name, label)
            if prompt is None:
                parts.append(f"{label}: none")
                same = False
            else:
                same = prompt.prompt == text
                parts.append(
                    f"{label}: v{prompt.version} ({'= code' if same else 'differs'})"
                )
            if label == PRODUCTION and not same:
                in_sync = False
        lines.append(f"{name}: " + ", ".join(parts))
    return lines, in_sync
