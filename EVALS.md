# Evals

Ten tasks: nine single items on real Nigerian and Kenyan procurement data, and one synthetic bid package. There are also four checks that need no model. Every task runs the full agent (Analyst, Challenger, recheck, draft) and is stopped at the approval gate with "reject", which also tests that nothing is saved without approval.

Run them with `uv run fairprice-evals --repeats 2 --model ollama:gemma4`. Raw results for every run, including failed rounds, are in `evals/results/`. The full tool-call trace of every run is in the `audit_log` table of `data/runs.sqlite` (not committed; regenerate by running the suite). Print one run with `uv run fairprice --audit <run id>`.

## The tasks

Expected answers come from bands we built by hand from records we read one by one (`evals/cases.json` lists them). Prices under review were set far from the band edges, so a pass cannot be luck at the boundary.

| Task | What must happen | Traps that must stay out of the band |
|---|---|---|
| `hilux-within` | NGN 95M Hilux (hand-checked median NGN 88.6M) is `within_band` | Bundles with a Prado or Land Cruiser |
| `hilux-above` | NGN 300M Hilux (3.4x) is `above_band` | Same bundles |
| `laptop-above` | NGN 10M laptop (hand-checked median NGN 2.6M) is `above_band` | Mixed lots with printers, servers, photocopiers |
| `laptop-within` | NGN 2.5M laptop is `within_band` | Same lots |
| `motorcycle-within` | NGN 1.3M motorcycle (hand-checked median NGN 1.31M) is `within_band` | "Empowerment" bundles of motorcycles, sewing and grinding machines |
| `aircon-traps` | Any assessment | Pickup trucks whose spec mentions AC, dormitory beds, demolition works, servicing contracts |
| `no-benchmark` | MRI scanner: only 2 published purchases, so `insufficient_comparables`, not a band padded with other equipment | |
| `kenya-no-quantities` | Kenya publishes no quantities, so no unit price; 0 records accepted | |
| `placeholder-dates` | Any assessment | Two ProBook records dated 2001-01-01, a NOCOPO placeholder |
| `bid-package` | `examples/bid-package.xlsx`, 3 lines and 3 invented bidders: Bidder B's NGN 300M Hilux is `above_band`, the other 7 quoted prices are `within_band`, the line Bidder C left out is `not priced`, and the output has no total, rank or recommendation column | Checks assessments only, not trap records (see weak spots) |
| no model | `write_price_review` without a person's approval only records a request; it refuses an approval for a different report, a report with no citations or an invented record id, and anything that is not a draft; an approval works only once; a rejected draft is deleted and cannot be approved; each format (xlsx, csv, json, and xlsx in mocked S3) goes from draft to approved and comes back from `get_price_review` as JSON | |
| no model | The bid file checks find exactly the 4 problems planted in the package: an arithmetic error, a changed quantity, a missing line, and two bidders with identical prices | |
| no model | The xlsx, csv and json templates read to the same package; packages with an unknown column, a price in words, a line not in the BoQ, or no BoQ are refused with their location | |

## Results

2 runs per task, on a MacBook (Apple M5, 16 GB), models run locally through Ollama. Round 7 is the latest code (`20261012-1238-ollama_gemma4.jsonl`). Round 6 (`20261012-1039-ollama_gemma4.jsonl`) is the same code before one fix to the self-check message (below). Qwen3 8B ran on round 3 code (`20261010-1321-ollama_qwen3_8b.jsonl`); we have not rerun it since.

| Task | Gemma 4, round 7 (latest) | Gemma 4, round 6 | Qwen3 8B, round 3 |
|---|---|---|---|
| hilux-within | **0/2** (right verdict, one bundle in the band) | 0/2 (wrong verdict) | 0/2 |
| hilux-above | 2/2 | 0/2 | 0/2 |
| laptop-above | 2/2 | 2/2 | 2/2 |
| laptop-within | 2/2 | 2/2 | 2/2 |
| motorcycle-within | 2/2 | 2/2 | 0/2 |
| aircon-traps | 2/2 | 2/2 | 2/2 |
| no-benchmark | 2/2 | 2/2 | 2/2 |
| kenya-no-quantities | 2/2 | 2/2 | 2/2 |
| placeholder-dates | 2/2 | 2/2 | 2/2 |
| **Single items** | **16/18** | 14/18 | 12/18 |
| bid-package | **2/2** | 0/2 | not run |
| Average time per single item | 243 s | 146 s | 359 s |
| Average time per bid package (3 lines) | 587 s | 621 s | not run |
| Model-free checks | 15/15 | 15/15 | 3/3 |

**Round 6 failed on every Hilux run, for two different reasons** (traced in the audit log):

- `hilux-within` (and the bid package's Hilux line): the first search found 10 records and the band had 7. Our self-check then asked for "one broader search (just the brand or model name)", and Gemma chose the brand: it searched "Toyota", kept Land Cruisers and Prados, and the median rose from NGN 78M to NGN 159M. A fair NGN 95M Hilux was reported as `below_band`. The wrong verdict came from our own prompt: it offered "the brand" as an option, while the other self-check message says to keep the one word that names the product. In round 5 the first searches found nothing, so this message never fired.
- `hilux-above`: the broader search was a sensible "Toyota Hilux", but Gemma kept the Hilux-and-Prado convoy bundles, and the Challenger did not remove them. The verdict (`above_band`) was right; the evidence was not.

**Round 7 fixed the first:** the broader-search message now asks for "just the word that names the product's model (such as 'hilux' or 'probook'), never the brand alone (such as 'toyota' or 'hp') and never a specification". Every Hilux verdict was right again (`hilux-within` medians NGN 94.2M against our hand-checked NGN 88.6M), and the bid package passed. The remaining failures are the second kind: in both `hilux-within` runs one Prado-and-Hilux bundle ([L111921]) stayed in the band. We have not fixed that (below).

**About the timings:** the laptop was in use during rounds 6 and 7 (and the unit tests ran during round 6), so many runs were slower than usual. Round 5 (85 s per item, 401 s per package) shows timings on an idle machine.

Qwen3 8B failed differently from Gemma 4: it reached the right verdict on every Hilux run, but let the Prado and Land Cruiser bundles into the band all four times, so the evidence behind the verdict was wrong. On motorcycles it kept only the one record that mentions "125cc". This is why the default model is now Gemma 4.

## Cost per run (measured)

Every run records what it cost (model calls, tokens, model time, and API fees when prices are set); `uv run fairprice --audit <run id>` shows it, and so does the report's JSON. Measured in round 8 (`20261013-0710-ollama_gemma4.jsonl`, one run per task, Gemma 4 on Ollama, MacBook Apple M5 16 GB):

| Task | Model calls | Input tokens | Output tokens | Model time |
|---|---|---|---|---|
| hilux-within | 7 | 7,022 | 540 | 21 s |
| hilux-above | 7 | 7,031 | 544 | 17 s |
| laptop-above | 7 | 23,014 | 966 | 42 s |
| laptop-within | 8 | 29,200 | 2,314 | 97 s |
| motorcycle-within | 8 | 34,476 | 2,014 | 114 s |
| aircon-traps | 8 | 37,318 | 2,458 | 137 s |
| no-benchmark | 6 | 7,669 | 513 | 25 s |
| kenya-no-quantities | 8 | 37,627 | 628 | 49 s |
| placeholder-dates | 12 | 50,928 | 1,358 | 129 s |
| **Single item, median** | **8** | **29,200** | **966** | **49 s** |
| bid-package (3 lines, 3 bidders) | 21 | 84,212 | 4,191 | 304 s |

**API fees: none.** The model runs on the laptop and no data leaves it. Input tokens dominate: each model call re-reads the conversation so far, including search results. A bid package costs about one single-item run per distinct line, however many bidders it has. The two Hilux runs were cheap because they failed early (see round 8 below), so a typical Hilux run costs more.

## How we got here

Each round fixed what the previous one exposed. We kept the failing rounds, because they show what the fixes are for.

| Round | Result | What failed | What we changed next |
|---|---|---|---|
| 0 | Qwen3 8B, Hilux only: 0/5 (two partial rounds) | With thinking on, at temperature 0, Qwen3 looped for 57 minutes before its first tool call. With thinking off, it searched, never re-checked, and gave up. A "search more broadly" nudge then made it search "pickup" instead of "hilux" and accept TATA pickups and 1,000 tricycles. The Challenger's SQL kept failing on a column it imagined ("adjusted"). | Thinking off, 4,096-token cap. A code self-check sends the Analyst back if it searched without re-checking. Code merges every record the model kept across all its checks. The nudge says keep the brand or model. The Challenger's prompt says adjusted prices are not stored, and to remove different products and bundles first. |
| 1 | Gemma 4: 14/18 | Both "above band" tasks. The Challenger over-corrected: it removed records for a large quantity, for being a single unit, or for being an outlier. On laptops it removed all 19. | The Challenger may remove only a different product, a bundle or a lot price, never a record because of its price, quantity or a short description. |
| 2 | Gemma 4: 13/18 | `hilux-above`: one narrow search left 7 records, and the Challenger then invented a reason ("from Kenya") to remove 4 of them. `motorcycle-within`: the Analyst rejected every motorcycle that did not state "125cc". | A band from one search with fewer than 10 records triggers a broader search. The Challenger's removals apply only if at least 5 records remain; otherwise its objections are shown to the committee. A missing spec is not a mismatch. |
| 3 | Gemma 4: 17/18 | One `hilux-within` run (below). | Not fixed in code. Added the bid package task and the model-free bid file check. |
| 4 | Gemma 4: 15/18 single items, bid package 0/2 | The package's motorcycle line failed in both runs. Told to keep "the most distinctive word", the Analyst chose "125cc" and searched it alone, and then kept only the one record that says "125cc". One `motorcycle-within` run crashed: `uvx` checked PyPI to start the borrowed server, and a brief network outage failed the DNS lookup. | The nudge now says to keep the word that names the product, never a specification such as "125cc". The borrowed server starts with `--offline` once it is cached. |
| 5 | Gemma 4: 17/18 single items, bid package 2/2 | One `motorcycle-within` run let an "empowerment" bundle into the band (below). | Not fixed. Added every Nigerian state publication to the data, and excluded the portal's test records. |
| 6 | Gemma 4: 14/18 single items, bid package 0/2 | Every Hilux run. The self-check's "broader search (just the brand or model name)" led Gemma to search "Toyota" (wrong verdict); separately, Gemma kept Hilux-and-Prado bundles (right verdict, wrong evidence). | The broader-search message now names the model ("hilux"), never the brand or a specification. |
| 7 | Gemma 4: 16/18 single items, bid package 2/2 | Both `hilux-within` runs kept one Prado-and-Hilux bundle; the verdicts were right. | Not fixed (below). After this round, one line was added to `find_comparables`'s description ("limit: … at most 50"). |
| 8 (latest, 1 run per task, to measure cost) | **Gemma 4: 5/9 single items, bid package 0/1** | Both Hilux runs and the package's Hilux line: Gemma searched whole phrases as one keyword ("Toyota Hilux double cabin"); the search matches each keyword as an exact piece of text, so every search found nothing and the runs ended `unresolved`. In round 7 its first search happened to split the phrase. The changed tool description is the most likely trigger: a small model at temperature 0 takes a different path after a one-line change. `motorcycle-within` and `aircon-traps` kept bundle and servicing records (the known failure below). | Make the search match each keyword's words in any order, so it no longer depends on how the model groups words; record a search plan and recover from empty searches. |

One more round was abandoned: the laptop slept mid-run, the connection to Ollama dropped, and the run hung with no timeout. It is kept as `20261010-0737-ollama_gemma4.jsonl` (1 run, failed). Every model call now has a 10-minute timeout, so a dropped connection fails one run instead of freezing the suite.

## Run-to-run variation

Measured, not hidden. Temperature is 0, but local inference is not perfectly deterministic.

- In round 5, 7 of 9 single-item tasks gave the same assessment, record count and median in both runs.
- `hilux-above` gave medians of NGN 78.3M and NGN 123.0M. Both runs were correctly `above_band` for NGN 300M, but a price near NGN 100M would have been judged differently by the two runs.
- `motorcycle-within` kept 12 records in one run and 7 in the other.
- In round 3, `hilux-within` broadened its search to "Toyota" in one run and got a median of NGN 159M; the other run got NGN 88.6M. Same input, opposite conclusions about the same price.
- Small differences in the prompt seem to change the path. The bid package's Hilux line uses the same item and price as `hilux-within`. The only difference we found in the prompt is that the price appears as `95000000.0` instead of `95000000`. Yet in a manual run of the package, that line kept three Prado bundles that `hilux-within` rejects.
- A committee should not rely on one run for a close call. Running twice and comparing is cheap.

## Weak spots in this test set

- **The Challenger says DISPUTED almost every time**, including on bands that are correct. Its removals are useful; its verdict carries little information yet.
- **`aircon-traps` and `placeholder-dates` pass without proving the band is right.** They test that trap records stay out, and accept any assessment. The air-conditioner median was NGN 4.0M in round 3 and NGN 1.55M in round 5. Both cannot be right, and lot or installation prices probably still enter that band. In round 5 the ProBook task found only 3 usable records, so it reported `insufficient_comparables`.
- **`bid-package` checks assessments, not the evidence behind them.** It does not check trap records. In the manual run described above, its Hilux line passed with three Prado bundles in the band.
- Ten tasks across five products is a small set. The hand-checked bands are our own reading of the records, not an official price list. The bid package is invented.

## Failures we did not fix

**Bundles kept in the band: `hilux-within`, round 7, both runs** (also `hilux-above` in round 6, and `motorcycle-within` in round 5). In round 7 the band included [L111921], "1 (one) Nr. Toyota Prado Vx4.0L ... & 1 (one) Nr. Toyota Hilux 4 WD DC": a Prado and a Hilux bought together, priced as two units of one item. The Analyst kept it, and the Challenger did not remove it. The verdict (`within_band`) was right, because one record moves a median of 8 only a little; the evidence was not, and with fewer records it could flip a verdict. In round 5 the same thing happened with an "empowerment" bundle of motorcycles, sewing machines and grinding machines.

**What we would try next:** a rule in code, not the model. Reject a record whose description names another model of the same brand (a Hilux search finding "Prado" or "Land Cruiser") or other kinds of products ("sewing machines" in a motorcycle search), and show the rule's reason in the report. The model is bad at this consistently, so it is a job for code. Separately, we would get the Challenger's removals through a structured tool call instead of parsing free text, so a long answer cannot lose them.

**Broadening to the brand (fixed in round 7, kept here because it can come back).** In round 3 the Analyst, on its own, searched "Toyota Hilux double cabin", then "Hilux double cabin", then "Toyota pickup", then just **"Toyota"**, and built its band from every Toyota it found. The median rose to NGN 159M, so a fair NGN 95M Hilux was reported as `below_band`. In round 6 our own self-check message caused the same thing. Round 7's message forbids the brand, but nothing in code stops the model choosing it anyway. Our first idea for a code rule was to accept a band only if every record mentions the item's rarest word, but the data shows why that fails: among Nigerian records with a unit price, "toyota" appears in 113, "hilux" in 53, and "pickup" in only 8, because most records spell it "pick-up". The rarest word is a spelling accident, not the product. Better candidates: let the officer name the anchor word (an optional `keywords` column in the BoQ), or rank words by how often they appear in the records the Analyst itself kept.

## Tried and rejected: a fast decision model for choosing comparable records

Most of a run's time goes on the Analyst deciding which past records describe the same item. "Decision models" such as TypeSafe AI's Jev claim to make that kind of choice up to 200 times faster than a chat model. Jev is a hosted service, so every record and quoted price would leave the machine, and we ruled it out. We tested an open-weights alternative, Deem-4B (`mertkayacs/deem-4b`, Apache-2.0, run through Ollama), against Gemma 4, before building anything on it.

**Test:** 88 records from our eval searches (Hilux, laptop, motorcycle, air conditioner, ProBook), including every trap record. We labelled each one by hand from its description as the same item, a bundle, a lot price, or a different item. Both models got the same question in Deem's own prompt format, one request per item for all its records. The script and raw answers are in `evals/classifier_experiment.py` and `evals/results/20261011-classifier-*.json`.

| | Deem-4B | Gemma 4 |
|---|---|---|
| Records labelled exactly right | 67 / 88 | 73 / 88 |
| Right keep-or-drop decision | 70 / 88 | 77 / 88 |
| Records that should be excluded but it would keep | 16 | 7 |
| Good records it would drop | 2 | 4 |
| Time for all 88 records | 56 s | 63 s |

**Why we rejected it:**

- **Less accurate where it matters.** Deem-4B would keep the two Hilux and Land Cruiser convoy bundles, several laptop-and-printer lots, and two dormitory beds found in the air-conditioner search. Those are the errors that put wrong evidence in front of a committee.
- **No faster for us.** The claimed speed comes from reading the decision from a single model pass, which needs the authors' own server. Through Ollama, Deem writes its answer token by token like any chat model, and the time goes on reading the long prompt.

**What we learned:**

- Gemma 4, asked once to label all records from a search in one structured request, took about 13 seconds per item. The agent takes 85 to 120 seconds, mostly reasoning over the same records across several turns. A `classify_records` tool on our MCP server, using Gemma, is our best lead for speed. It is not built, because we would have to rerun every eval to show it keeps accuracy.
- Both models kept the "lot price" records, where the quantity is 1 but the description says many units ("Supply of 87 laptop computers"). A code rule may catch those better than either model.
- Small test, labelled by us: 88 records from 5 items.
