"""Tried and rejected (EVALS.md): can a fast decision model label comparable records better than Gemma 4?

Usage: uv run python evals/classifier_experiment.py data/fairprice.duckdb mertkayacs/deem-4b
Labels below were set by hand from each record's description. Needs Ollama with the model pulled.
"""

import json, sys, time, urllib.request, duckdb
S, B, L, D = "same", "bundle", "lot", "different"
LABELS = {  # hand labels, read from each description
 "Toyota Hilux double cabin pickup": {111921:B,112029:S,112063:S,112073:S,113240:B,113245:B,114843:S,115386:S,116222:S,116396:S,116455:S,117321:S,120992:B,122388:S,123662:B,124476:S},
 "laptop computer": {109838:B,110848:B,110849:B,110850:B,112427:S,112428:S,112429:S,113506:S,114847:S,115075:B,116683:S,117122:B,117170:B,117259:L,117305:B,117306:L,117989:L,118475:B,118656:B,121004:B,121028:L,121570:B,122097:B,123007:B,124445:S,124486:B,126169:S,126225:S},
 "motorcycle 125cc": {109372:S,109690:B,109764:B,109765:B,109946:B,110443:S,114137:S,114138:D,114164:S,114172:S,114187:D,115097:B,119366:B,120597:B,120614:B,120618:B,125185:B,125327:S,125332:S,125333:S,125336:S,125337:S,125641:S},
 "1.5HP split air conditioner": {109837:D,110754:S,111021:B,112479:S,113511:D,114093:B,114148:D,114149:D,115038:D,117551:B,117626:S,120382:D,121569:D,123960:D,124442:S,124486:B},
 "HP ProBook 440 laptop": {113472:S,113473:S,113480:B,113628:S,120373:D},
}
CRITERIA = {
 S: "The same kind of item as the item under review. A record that does not state a specification is still the same item; only a stated, different specification makes it different.",
 B: "A purchase that mixes this item with other kinds of items, services or works, so its price is not the price of this item alone.",
 L: "The quantity is 1, but the description says several units (for example plural items, 'to staff', 'to students'), so the price covers many items.",
 D: "A different kind of item, a different model, or a stated specification that contradicts the item under review.",
}
db = duckdb.connect(sys.argv[1], read_only=True)
SYSTEM = ("You are a careful decision assistant. Use the state and decision schema in the user message to make the requested "
          "decisions. For every field, choose exactly one answer symbol (e.g. A, B, C, ...) from its listed options and return "
          "one valid JSON object mapping each field name to its chosen symbol. Use the field names and symbols exactly as given. "
          "Do not include explanations, Markdown, or extra text.")
def prompt(item, ids):
    recs = {f"r{i}": f"{d} | quantity {q:g}" for i, q, d in db.execute(
        f"SELECT id, quantity, description FROM lines WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()}
    lines = []
    for f in recs:
        lines.append(f"{f}: Compared with the item under review, what is record {f}?")
        lines += [f"    {sym} = {k}: {v}" for sym, (k, v) in zip("ABCD", CRITERIA.items())]
    state = json.dumps({"item_under_review": item, "records": recs}, ensure_ascii=False, indent=2)
    return ("Return one answer for every field using the supplied answer symbols.\n\n## State\n" + state +
            "\n## Decision schema\n" + "\n".join(lines))
def ask(model, user):
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    if "gemma" in model: body["think"] = False
    req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.perf_counter(); out = json.load(urllib.request.urlopen(req, timeout=900)); return out, time.perf_counter() - t
model = sys.argv[2]
ask(model, prompt("laptop computer", [112427]))  # warm-up: load the model
res = {}
for item, labels in LABELS.items():
    out, secs = ask(model, prompt(item, sorted(labels)))
    try: got = json.loads(out["message"]["content"])
    except Exception: got = {}
    sym = dict(zip("ABCD", CRITERIA))
    pred = {i: sym.get(str(got.get(f"r{i}", "")).strip()[:1], "missing") for i in labels}
    res[item] = {"secs": round(secs, 1), "pred": pred, "eval_tokens": out.get("eval_count"), "prompt_tokens": out.get("prompt_eval_count")}
    print(f"{model:<20} {item:<34} {secs:6.1f}s  prompt {out.get('prompt_eval_count')} tok, output {out.get('eval_count')} tok", flush=True)
json.dump({"labels": {k: {str(i): v for i, v in d.items()} for k, d in LABELS.items()},
           "res": {k: {**v, "pred": {str(i): p for i, p in v["pred"].items()}} for k, v in res.items()}},
          open(f"evals/results/classifier-{model.replace('/', '_').replace(':', '_')}.json", "w"))
