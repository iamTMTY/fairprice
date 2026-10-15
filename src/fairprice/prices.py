"""What a model's tokens cost on a hosted API, from OpenRouter's public price list (no key needed).

For an API model it is the run's fee; for a local model it is what the run would have cost hosted: the saving.
A local model is priced as its closest hosted relative, e.g. Ollama's gemma4 as the smallest Gemma 4 on
OpenRouter. The list is cached in data/model-prices.json and refreshed weekly, so runs stay offline otherwise.
FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS / _OUTPUT_TOKENS (e.g. a negotiated price) override it.
"""

import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "model-prices.json"
URL = "https://openrouter.ai/api/v1/models"
MAX_AGE_DAYS = 7


def fetch():
    """{"fetched": date, "models": {id: [USD per input token, USD per output token]}}, without free tiers."""
    with urllib.request.urlopen(URL, timeout=15) as r:
        listed = json.loads(r.read())["data"]
    models = {}
    for m in listed:
        try:
            price = [float(m["pricing"]["prompt"]), float(m["pricing"]["completion"])]
        except (KeyError, TypeError, ValueError):
            continue
        if not m["id"].endswith(":free") and min(price) >= 0 and max(price) > 0:
            models[m["id"]] = price
    return {"fetched": dt.date.today().isoformat(), "models": models}


def table():
    """The cached price list, refreshed when older than a week; None if it cannot be had."""
    try:
        cached = json.loads(CACHE.read_text())
        if (dt.date.today() - dt.date.fromisoformat(cached["fetched"])).days <= MAX_AGE_DAYS:
            return cached
    except (OSError, ValueError, KeyError):
        cached = None
    try:
        fresh = fetch()
    except (OSError, ValueError, KeyError):
        return cached  # offline: an old list is better than none
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(fresh))
    return fresh


def plain(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def size(name):
    found = re.search(r"(\d+(?:\.\d+)?)b\b", name.lower())
    return float(found[1]) if found else None


def closest(model, models):
    """The OpenRouter id priced for a FairPrice model ("ollama:gemma4", "openai:google/gemma-4-26b-a4b-it"): the
    same model if listed, else the same family at the nearest size (the smallest if the size is not known)."""
    name = model.split(":", 1)[1] if ":" in model else model
    if name in models:
        return name
    base, _, tag = name.partition(":")  # Ollama: family:tag, e.g. qwen3:8b
    wanted = plain(base.rsplit("/", 1)[-1])
    exact = [i for i in models if plain(i.rsplit("/", 1)[-1]) == wanted]
    if exact:
        return min(exact, key=lambda i: sum(models[i]))
    family = [i for i in models if plain(i.rsplit("/", 1)[-1]).startswith(wanted)]
    if not family:
        return None
    target = size(tag) or size(base) or 0
    return min(family, key=lambda i: (abs((size(i) or 1e9) - target), sum(models[i])))


def is_local(model):
    """Ollama, or an OpenAI-compatible server on this computer, costs nothing per token."""
    if model.startswith("ollama:"):
        return True
    host = urlparse(os.environ.get("FAIRPRICE_MODEL_URL", "")).hostname or ""
    return model.startswith("openai:") and (host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"))


def price(model):
    """(USD per input token, USD per output token, what it is priced as), or None if no price is known."""
    manual = [float(os.environ.get(f"FAIRPRICE_PRICE_PER_MILLION_{k}_TOKENS") or 0) for k in ("INPUT", "OUTPUT")]
    if any(manual):
        return manual[0] / 1e6, manual[1] / 1e6, "your price"
    listed = table()
    found = closest(model, listed["models"]) if listed else None
    return (*listed["models"][found], f"{found} on OpenRouter, {listed['fetched']}") if found else None


def money(amount):
    """$0.28; a fraction of a cent keeps its first figures ($0.0042) rather than showing $0.00."""
    return f"${amount:.2f}" if amount == 0 or amount >= 0.01 else f"${amount:.2g}"
