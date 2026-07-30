"""Shared experiment lifecycle and telemetry helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable


class ExperimentInfrastructureError(RuntimeError):
    """Raised when a task environment cannot be prepared or cleaned up."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        raise ExperimentInfrastructureError(
            f"command timed out after {timeout}s: {' '.join(command)}\n{output[-12000:]}"
        ) from exc


def image_label(image: str, label: str) -> str | None:
    result = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            f'{{{{ index .Config.Labels "{label}" }}}}',
            image,
        ],
        30,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_task_image(
    *,
    image: str,
    task_id: str,
    base_commit: str,
    dockerfile: Path,
    context: Path,
    build_timeout_seconds: int,
    precompile: bool = False,
) -> dict[str, Any]:
    """Build and smoke-test a task image when the correct image is absent."""

    expected_precompile = "true" if precompile else "false"
    reused = (
        image_label(image, "benchmark.base_commit") == base_commit
        and image_label(image, "benchmark.precompile") == expected_precompile
    )
    started = time.monotonic()
    if not reused:
        build = _run(
            [
                "docker",
                "build",
                "--file",
                str(dockerfile.resolve()),
                "--build-arg",
                f"RUFF_COMMIT={base_commit}",
                "--build-arg",
                f"PRECOMPILE={expected_precompile}",
                "--label",
                f"benchmark.task_id={task_id}",
                "--label",
                f"benchmark.base_commit={base_commit}",
                "--label",
                f"benchmark.precompile={expected_precompile}",
                "--tag",
                image,
                str(context.resolve()),
            ],
            build_timeout_seconds,
        )
        if build.returncode != 0:
            raise ExperimentInfrastructureError(
                f"image build failed for {task_id}: {build.stdout[-12000:]}"
            )

    smoke = _run(
        ["docker", "run", "--rm", "--network", "none", image, "smoke-test"],
        120,
    )
    if smoke.returncode != 0:
        raise ExperimentInfrastructureError(
            f"image smoke test failed for {task_id}: {smoke.stdout[-12000:]}"
        )
    inspected = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
        ],
        30,
    )
    if inspected.returncode != 0:
        raise ExperimentInfrastructureError(
            f"could not inspect image {image}: {inspected.stdout[-12000:]}"
        )
    return {
        "image": image,
        "image_id": inspected.stdout.strip(),
        "image_reused": reused,
        "image_prepare_seconds": round(time.monotonic() - started, 3),
        "base_commit": base_commit,
        "precompile": precompile,
    }


def _remove_docker_resource(
    command: list[str],
    *,
    absent_message: str,
    attempts: int = 4,
) -> str | None:
    """Remove a Docker resource, tolerating short container-teardown races."""

    last_output = ""
    for attempt in range(attempts):
        result = _run(command, 120)
        last_output = result.stdout
        if result.returncode == 0 or absent_message in result.stdout.lower():
            return None
        if attempt + 1 < attempts:
            time.sleep(0.5 * (attempt + 1))
    return last_output[-12000:]


def remove_image(image: str) -> str | None:
    return _remove_docker_resource(
        ["docker", "image", "rm", image],
        absent_message="no such image",
    )


def remove_image_containers(image: str) -> str | None:
    """Remove stopped or running benchmark containers derived from one task image."""

    listed = _run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"ancestor={image}"],
        30,
    )
    if listed.returncode != 0:
        return listed.stdout[-12000:]
    container_ids = listed.stdout.split()
    if not container_ids:
        return None
    removed = _run(["docker", "rm", "--force", *container_ids], 120)
    if removed.returncode == 0:
        return None
    return removed.stdout[-12000:]


def remove_volume(volume: str) -> str | None:
    return _remove_docker_resource(
        ["docker", "volume", "rm", volume],
        absent_message="no such volume",
    )


def trajectory_metrics(trajectory_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider usage that mini-swe-agent stores per response."""

    info = trajectory_data.get("info")
    info = info if isinstance(info, dict) else {}
    model_stats = info.get("model_stats")
    model_stats = model_stats if isinstance(model_stats, dict) else {}
    messages = trajectory_data.get("messages")
    messages = messages if isinstance(messages, list) else []

    provider_responses: list[dict[str, Any]] = []
    assistant_turns = 0
    tool_calls = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        assistant_turns += 1
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            tool_calls += len(calls)
        extra = message.get("extra")
        response = extra.get("response") if isinstance(extra, dict) else None
        if isinstance(response, dict):
            provider_responses.append(response)

    usage_totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    providers: set[str] = set()
    provider_models: set[str] = set()
    response_cost = 0.0
    missing_cost_responses = 0
    for response in provider_responses:
        provider = response.get("provider")
        model = response.get("model")
        if isinstance(provider, str) and provider:
            providers.add(provider)
        if isinstance(model, str) and model:
            provider_models.add(model)
        usage = response.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage_totals[key] += int(value)
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached = prompt_details.get("cached_tokens")
            if isinstance(cached, (int, float)) and not isinstance(cached, bool):
                usage_totals["cached_tokens"] += int(cached)
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning = completion_details.get("reasoning_tokens")
            if isinstance(reasoning, (int, float)) and not isinstance(reasoning, bool):
                usage_totals["reasoning_tokens"] += int(reasoning)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            response_cost += float(cost)
        else:
            missing_cost_responses += 1

    api_calls = model_stats.get("api_calls")
    steps_used = (
        int(api_calls)
        if isinstance(api_calls, (int, float)) and not isinstance(api_calls, bool)
        else len(provider_responses) or assistant_turns
    )
    instance_cost = model_stats.get("instance_cost")
    cost_usd = (
        float(instance_cost)
        if isinstance(instance_cost, (int, float)) and not isinstance(instance_cost, bool)
        else response_cost
    )
    return {
        "steps_used": steps_used,
        "assistant_turns": assistant_turns,
        "tool_calls": tool_calls,
        "provider_response_count": len(provider_responses),
        "prompt_tokens": usage_totals["prompt_tokens"],
        "completion_tokens": usage_totals["completion_tokens"],
        "total_tokens": usage_totals["total_tokens"],
        "cached_tokens": usage_totals["cached_tokens"],
        "reasoning_tokens": usage_totals["reasoning_tokens"],
        "cost_usd": cost_usd,
        "cost_complete": bool(provider_responses) and missing_cost_responses == 0,
        "providers": sorted(providers),
        "provider_models": sorted(provider_models),
    }


def cleanup_errors(errors: Iterable[str | None]) -> list[str]:
    return [error for error in errors if error]
