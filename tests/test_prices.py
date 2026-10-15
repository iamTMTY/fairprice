"""Model prices: the closest hosted model, the cached list, local models and the money format."""

import datetime as dt
import json

from fairprice import prices

LISTED = {"google/gemma-4-26b-a4b-it": [9e-8, 3e-7], "google/gemma-4-31b-it": [2e-7, 4e-7],
          "qwen/qwen3-8b": [5e-8, 1e-7], "qwen/qwen3-32b": [1e-7, 3e-7], "openai/gpt-4o-mini": [1.5e-7, 6e-7]}


def test_the_closest_hosted_model():
    assert prices.closest("ollama:gemma4", LISTED) == "google/gemma-4-26b-a4b-it"  # no size: the smallest of the family
    assert prices.closest("ollama:gemma4:latest", LISTED) == "google/gemma-4-26b-a4b-it"
    assert prices.closest("ollama:qwen3:30b", LISTED) == "qwen/qwen3-32b"  # the nearest size
    assert prices.closest("ollama:qwen3:8b", LISTED) == "qwen/qwen3-8b"
    assert prices.closest("openai:google/gemma-4-31b-it", LISTED) == "google/gemma-4-31b-it"  # an OpenRouter id
    assert prices.closest("openai:gpt-4o-mini", LISTED) == "openai/gpt-4o-mini"
    assert prices.closest("anthropic:claude-x", LISTED) is None


def test_local_models(monkeypatch):
    assert prices.is_local("ollama:gemma4")
    monkeypatch.setenv("FAIRPRICE_MODEL_URL", "http://localhost:1234/v1")
    assert prices.is_local("openai:gemma-4")
    monkeypatch.setenv("FAIRPRICE_MODEL_URL", "https://openrouter.ai/api/v1")
    assert not prices.is_local("openai:google/gemma-4-26b-a4b-it") and not prices.is_local("anthropic:x")


def test_the_price_list_is_cached_and_refreshed_weekly(tmp_path, monkeypatch):
    monkeypatch.setattr(prices, "CACHE", tmp_path / "prices.json")
    fetched = []
    monkeypatch.setattr(prices, "fetch", lambda: fetched.append(1) or {"fetched": dt.date.today().isoformat(), "models": LISTED})
    assert prices.table()["models"] == LISTED and len(fetched) == 1
    assert prices.table()["models"] == LISTED and len(fetched) == 1  # from the cache
    old = {"fetched": (dt.date.today() - dt.timedelta(days=30)).isoformat(), "models": {"x/old": [1e-7, 1e-7]}}
    prices.CACHE.write_text(json.dumps(old))
    assert prices.table()["models"] == LISTED and len(fetched) == 2  # refreshed

    def offline():
        raise OSError("no network")
    prices.CACHE.write_text(json.dumps(old))
    monkeypatch.setattr(prices, "fetch", offline)
    assert prices.table() == old  # an old list beats none
    prices.CACHE.unlink()
    assert prices.table() is None and prices.price("ollama:gemma4") is None


def test_fetch_reads_openrouters_list(monkeypatch):
    body = {"data": [{"id": "google/gemma-4-26b-a4b-it", "pricing": {"prompt": "0.00000009", "completion": "0.0000003"}},
                     {"id": "google/gemma-4-26b-a4b-it:free", "pricing": {"prompt": "0", "completion": "0"}},
                     {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
                     {"id": "broken"}]}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(body).encode()
    monkeypatch.setattr(prices.urllib.request, "urlopen", lambda url, timeout: Reply())
    assert prices.fetch()["models"] == {"google/gemma-4-26b-a4b-it": [9e-8, 3e-7]}


def test_money():
    assert [prices.money(x) for x in (0, 0.28, 1.5, 0.00423, 0.01)] == ["$0.00", "$0.28", "$1.50", "$0.0042", "$0.01"]
