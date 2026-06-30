import asyncio

from app.core.cache import get_cached, parse_cached_providers, set_cache
from app.models import EmailCache
from app.providers.registry import get_all_providers
from app.schemas import ProviderResult

_VERDICT_WEIGHT = {"valid": 0, "risky": 1, "unknown": 2, "invalid": 3}


def _majority_vote(results: dict[str, ProviderResult]) -> str:
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r.status] = counts.get(r.status, 0) + 1
    if not counts:
        return "unknown"
    top = max(counts, key=lambda k: (counts[k], -_VERDICT_WEIGHT.get(k, 99)))
    return top


async def validate_with_cache(
    email: str,
    provider_names: list[str],
    strategy: str,
    ttl_days: int | None = None,
) -> tuple[str, dict[str, ProviderResult], EmailCache | None]:
    """Validate with cache check. Returns (verdict, providers, cache_row_if_hit)."""
    cached = get_cached(email)
    if cached:
        return cached.verdict, parse_cached_providers(cached), cached

    verdict, providers = await validate(email, provider_names, strategy)
    # ttl_days=0 means caller explicitly opted out of caching
    if verdict != "unknown" and ttl_days != 0:
        set_cache(email, verdict, providers, strategy, ttl_days=ttl_days)
    return verdict, providers, None


async def validate(
    email: str,
    provider_names: list[str],
    strategy: str,
) -> tuple[str, dict[str, ProviderResult]]:
    providers = get_all_providers()
    selected = {n: providers[n] for n in provider_names if n in providers}

    if not selected:
        return "unknown", {}

    if strategy == "bouncify_only":
        # Free local pre-filter: skip the paid Bouncify credit for emails
        # that are objectively invalid (bad syntax or no MX/A record).
        # Bouncify charges 1 credit per call and would return `invalid` for
        # those too, so this is pure savings with no accuracy loss. Local
        # is in the registry whether or not it was selected — using it here
        # as a free gate doesn't break the "only Bouncify" intent.
        local = providers.get("local")
        if local:
            local_result = await local.verify(email)
            if local_result.status == "invalid":
                return "invalid", {"local": local_result}
        p = selected.get("bouncify") or next(iter(selected.values()))
        result = await p.verify(email)
        return result.status, {p.name: result}

    if strategy == "local_first":
        local = selected.get("local")
        local_result = None
        if local:
            local_result = await local.verify(email)
            if local_result.status == "invalid":
                return "invalid", {"local": local_result}
        remaining = {n: p for n, p in selected.items() if n != "local"}
        if not remaining:
            if local_result is None:
                return "unknown", {}
            return local_result.status, {"local": local_result}
        results: dict[str, ProviderResult] = {}
        if local_result is not None:
            results["local"] = local_result
        tasks = {name: p.verify(email) for name, p in remaining.items()}
        done = await asyncio.gather(*tasks.values())
        for name, result in zip(tasks.keys(), done):
            results[name] = result
        return _majority_vote(results), results

    if strategy == "consensus":
        tasks = {name: p.verify(email) for name, p in selected.items()}
        done = await asyncio.gather(*tasks.values())
        results = dict(zip(tasks.keys(), done))
        return _majority_vote(results), results

    if strategy == "waterfall":
        order = ["local", "hunter", "bouncify", "zerobounce", "neverbounce"]
        all_results: dict[str, ProviderResult] = {}
        for name in order:
            if name not in selected:
                continue
            result = await selected[name].verify(email)
            all_results[name] = result
            if result.status in ("valid", "invalid"):
                return result.status, all_results
        return _majority_vote(all_results) if all_results else "unknown", all_results

    # Default fallback
    p = next(iter(selected.values()))
    result = await p.verify(email)
    return result.status, {p.name: result}
