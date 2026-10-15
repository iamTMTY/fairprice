# Architecture

![FairPrice architecture](docs/fairprice-architecture.png)

## Agent shape

A LangGraph state graph with six nodes. Every review is one **run** (see [Runs](#runs-one-review-across-many-tool-calls)): the tools record each step under the run's id, and the report is built only from those records. LangGraph's own state is checkpointed in SQLite (`data/runs.sqlite`), so a run paused at the person's decision can be resumed later with `--resume <run id>`.

| Node | What it does | Model? |
|---|---|---|
| **Analyst** | First records a search plan (`plan_searches`): the keyword sets it will try, most specific first. Then searches past awards, decides which records describe the same product, rejects bundles, lot prices and different specifications with a reason, and moves to the next planned search when a search is empty or thin. | Yes |
| **Self-check** (inside Analyst) | Sends the Analyst back if it did not plan, ran a search it never price-checked, found nothing, or stopped with a thin band or too few comparables; each time it names the next search in the Analyst's own plan. Then merges every record it kept across all checks into one final check. | No |
| **Challenger** | A second agent, with different tools, that tries to disprove the band using its own SQL. It names records to remove and gives a verdict: STANDS or DISPUTED, recorded in the run with `challenge_review`. | Yes |
| **Recheck** | Rebuilds the band without the records the Challenger removed. | No |
| **Summary** | Builds the summary a person reads (`report.py`) from the run's recorded steps. | No |
| **Decision** | `interrupt()`: a person picks approve (with their full name), revise (with questions, which restarts the Analyst) or reject from a menu. | Human |
| **Save** | Calls `write_price_review(run_id, approved_by)` with that person's name; the tool builds the report from the run's recorded steps only, as data in the `reports` table and as files in the chosen format (xlsx by default, csv or json), with "Approved by <name> on <date>". It cites past purchases by the publisher's own record ID (OCID and contract ID), and says for every source whether it was used and, if not, why. The model writes no numbers. | No |

The model judges; code counts. Every number in a report comes from a tool output, and every past purchase is cited by its official record ID.

### Bid packages (`--package`)

`batch.py` reads a bid package (xlsx, csv or json) into one form and checks it against `bid_package.schema.json`. Spreadsheet columns are those of a standard price schedule; a fixed table of headings maps each to one field ("Rate in KES (VAT inclusive)" is the unit price), and anything else is refused. It checks the rules the schema cannot express (unique BoQ lines and bidders, every bid line in the BoQ). A package that fails is refused with each problem's location; nothing is interpreted. Each row keeps its location (sheet and row, csv row, or JSON path) so findings can cite it. The whole package is one run, and each BoQ line is a line of that run. It runs the same graph once per distinct item, paused after Recheck, so it reuses the same evidence and the same evals. Each line's steps are recorded as they happen, so an interrupted package resumes (`--resume <run id>`) from the lines that finished. One failed line is reported and does not stop the batch.

Code then does four things:

- compares every bidder's unit price with that item's band;
- checks the bid files: arithmetic errors, missing lines, quantities that differ from the BoQ, and identical prices across bidders. Findings state facts, not any country's law: what they mean is for the committee under its own rules;
- shows each quote against the line's budget, if the BoQ has one;
- builds one report for the package.

There is one decision for the whole package, and `revise` can redo chosen lines. On approval, `write_price_review` writes it as for a single item. The output never totals or ranks bidders. The Analyst is shown the median quoted price, not any one bid, so no single bidder steers which records it picks.

## MCP servers

| Server | Built or borrowed | Tools and resources | Used by |
|---|---|---|---|
| `fairprice-mcp` (`src/fairprice/server.py`), **the product** | Built, with the official MCP Python SDK; stdio or streamable HTTP | Runs: `start_review`, `start_package_review` (JSON or an uploaded workbook). Steps: `plan_searches`, `find_comparables`, `check_price_comparability`, `challenge_review`. FairPrice's own agent as a service: `review_with_fairprice`, `review_status`. Reports: `write_price_review` (the action tool), `get_price_review`. Data: `list_countries`, `add_country`. Resources: the tender format, an example, a blank workbook, and `ocds://line/{id}` | Other MCP clients (procurement systems, AI apps); FairPrice's CLI; FairPrice's own agent (Analyst, Challenger, Recheck, Save) |
| `mcp-server-motherduck==1.1.0` | Borrowed (MotherDuck, open source) | `execute_query`, `list_tables`, `list_columns`, `list_databases`; read-only by default for local files | Challenger |

**Why borrow the DuckDB server:** it is a maintained SQL server with a read-only mode, row limits and query timeouts already built in. Writing our own would add risk and no value. Keeping it separate also means the Challenger cannot simply rerun the Analyst's tools to confirm the Analyst's conclusion. It needs MCP SDK 2.x while our LangChain adapter needs 1.x, so it runs in its own environment through `uvx`. Once it is cached it starts with `--offline`, because a network blip during one eval run once failed the run when `uvx` checked PyPI.

### The server is the product; the CLI is one client

Everything a review needs is behind the server, so it can be deployed as a web service that other systems build on:

- **Audit where the work happens.** Every tool goes through one wrapper (`server.tool`) that writes the audit row (inputs, output, time, organisation, caller) in the server, whoever called. The CLI only logs calls to the borrowed DuckDB server, which is not ours.
- **Access keys and organisations** (`--http`). Each key belongs to one organisation. Runs, reports and audit rows carry their organisation, and every query is scoped to the caller's (`db.org()`), so one ministry never sees another's bid prices. On one computer (stdio) everything belongs to "local".
- **Approval belongs to the app.** The app shows a person the review and, if they approve, calls `write_price_review` with their name. FairPrice does not try to verify people: who may approve is the app's business, as everything else about its users is. What FairPrice guarantees is that the report contains only what the review's own tool calls found, and the audit log records which app wrote it and when.
- **Workbooks travel through MCP.** `start_package_review` takes the file itself (base64) and reads it exactly as the CLI does, so problems are reported with their sheet and row.
- **FairPrice's own agent is a background service.** `review_with_fairprice` starts `fairprice --review <run>` as a separate process, working as the caller's organisation, and records its progress in the run (`status` steps, which do not count as changes to the review); `review_status` reports it. Apps that cannot reason themselves get the full Analyst–Challenger review this way.
- **Files by link.** Report files come back as signed links valid for one hour (`/reports/...`), or S3's own.
- **The CLI uses the same doors.** With `FAIRPRICE_SERVER_URL`, `fairprice` uploads the package, starts the server's review, follows its status, and asks the person at the terminal to approve or reject: nothing private.

### Runs: one review across many tool calls

`start_review` (one item and its quoted price) or `start_package_review` (a bid package as JSON) creates a run and returns its `run_id`. Every other step takes that id and is recorded in the `run_steps` table: the plan, each search (its keywords and the record ids it returned), each price check (the ids judged comparable, the ids rejected with reasons, and the tool's full result), the second check, and the run's cost. A caller passes only judgements, never facts:

- `check_price_comparability(run_id, candidate_ids, rejected)`: the quoted price, currency, year and country come from the run, not the caller. Ids the run's own searches never returned are left out of the band and named in the answer (`left_out`), so every record in a band was found by a recorded search, and one mistyped id does not throw away the rest (round 9).
- `challenge_review(run_id, verdict, reason, objections)`: objections must name records in the line's band.
- `write_price_review(run_id, approved_by)`: no report body is passed in. The tool builds the report from the run's steps, checks that every cited record exists, and writes it under the approver's name.

Because every step is stored, any run can be rebuilt after the fact (`server.review(run_id)`); the eval suite checks that a rebuilt run gives the same result as the live one. An app that is not FairPrice can do a whole review with these tools alone, and its report has the same guarantees as ours.

## Plan, act, check, recover, human

Where each part of agent behaviour lives, and the eval round that showed why it is needed ([EVALS.md](EVALS.md#how-we-got-here)).

| Behaviour | Where | What it does | Evidence |
|---|---|---|---|
| **Plan** | Analyst → `plan_searches` | Before searching, the Analyst records the keyword sets it will try, most specific first, and what it will reject. The plan is stored in the run and shown in the report for each line. | Round 8: whole-phrase searches found nothing and runs ended unresolved; the plan gives the recovery a next step that is the model's own. |
| **Act** | `find_comparables`, `check_price_comparability`, and the Challenger's `execute_query` | Search, judge comparability, build the band. Code adjusts prices for inflation and currency and computes the band; the model only judges which records are the same product. | Rounds 0 to 8. |
| **Check** | Self-check (code), Challenger (second model, different tools), Recheck (code) | The self-check refuses to finish an unchecked or thin result. The Challenger tries to disprove the band with its own SQL. Recheck applies its removals only if at least 5 records remain; otherwise they are shown as objections. | Round 1 (Challenger over-removed), round 2 (thin band from one search). |
| **Recover** | Self-check messages; step budgets; failed-line isolation | An empty, thin or insufficient search sends the Analyst to the next search in its plan (or a one-word search if the plan is used up). Step budgets end a stuck agent as `unresolved` instead of looping. One failed package line is reported and the rest continue. Every model call has a 10-minute timeout. | Round 0 (loops), round 4 (crashed run), round 6 (a recovery message that steered to the brand), round 8 (empty searches). |
| **Human** | The person's decision (`interrupt`); `write_price_review(approved_by)` | FairPrice never ranks, recommends or decides: it produces evidence. A person reads the summary and approves (the report is written under their name), revises (questions back to the Analyst) or rejects (nothing is written). | Every round. |

**The search no longer depends on how the model groups words.** `find_comparables` matches every word of the keywords in any order, so `["hilux pickup"]`, `["pickup", "hilux"]` and `["Pickup Hilux"]` are the same search (round 8's failure).

**The human stays in charge because FairPrice only produces evidence.** It never totals, ranks or recommends bidders and never decides an award; the committee decides in its own process. A report carries the name of the person who approved it, given by them in the CLI or by the app they use.

## Data

`fairprice-ingest` downloads OCDS publications from the OCP Data Registry, loads the raw JSON into DuckDB unchanged (`releases`), and flattens them into one `lines` table. It also fetches the World Bank consumer price index (`FP.CPI.TOTL`) and exchange rates (`PA.NUS.FCRF`) for every loaded country. After that, the agent runs offline.

**Adding a country** (`fairprice-ingest --add RW`, or the MCP tool `add_country`) loads every publication the country has on the registry. The registry has no API, so the ingest reads its search page, where each publication has a flag image named with its ISO country code, a "Country: Publisher" title and an id. A country's publications are downloaded unchanged and joined end to end into one file per country (`data/raw/nigeria.jsonl.gz`); joined gzip files read back as one. `data/raw/manifest.json` records which publications each file holds and in what order, so a rebuild needs no network. Records are told apart by their OCID prefix, which each publisher registers once (`ocds-gyl66f` is the federal BPP, `ocds-zinqhl` is Ebonyi State). The `sources` table records each publication's registry title (used as "Published by" in reports), its currency (the most common currency in its awards), and how it is read:

- **item_prices**: at least half the contract items carry a unit price (Rwanda). One line per priced item.
- **items**: at least half the contracts list an item quantity (Nigeria). One line per contract, priced when it has one item.
- **awards**: neither (Kenya). One line per award, with no unit price.

**Stable record ids:** each publication's lines get one block of ids, numbered after the publications already loaded, in the order they were added. The eval cases refer to records by id, so adding Ghana must not renumber Nigeria. We checked that rebuilding Nigeria and Kenya with this code reproduces all 126,281 records with the same ids. Ids still change if a publication's own data changes when it is downloaded again.

| Source | Records | What it can support |
|---|---|---|
| Nigeria, Bureau of Public Procurement (NOCOPO), 2021-2026 | 98,866 processes, 17,044 contracts, 12,526 with a unit price | Unit prices: contract value / item quantity, single-item contracts only |
| Kenya, PPRA (PPIP), 2018-2026 | 260,744 processes, 109,237 awards | Award totals only. Kenya publishes no items or quantities, so it cannot give unit prices. |
| Nigeria, 11 state portals and Budeshi | 2,396 records | 1,322 with a unit price (Ebonyi, Enugu, Osun, Abia, Anambra); the rest publish no quantities |
| Kenya, Makueni County | 728 awards | Award totals only |

**Which half we built on:** the real-data half, meaning award history and prices. The portals publish no bid documents, so FairPrice benchmarks prices under review. It reads bidders' price schedules (spreadsheets), not full bid documents, and the bid packages in our examples and evals are synthetic.

## Messy data

The brief says the data will not be cleaned for us, and that handling it is most of criterion one. FairPrice uses the data as published and handles each problem in code, with a reason the committee can read. The table of problems, how often each occurs in the Nigerian and Kenyan data, and what FairPrice does is in the README: [Data quality](README.md#data-quality-what-the-published-data-gets-wrong-and-what-fairprice-does). In code: `server.data_flags` (record flags), `server.price_band` (duplicates, extreme values, inflation and currency), `ingest.TABLES_SQL` (buyer names).



## Models

The default is **Gemma 4 8B** (open weights) through Ollama, run locally with thinking mode off and a 4,096-token output cap. We started with Qwen3 8B, as planned, but switched after the evals. On the same code, Gemma 4 passed 17 of 18 single-item runs at about 2 minutes each. Qwen3 8B passed 12 of 18, kept letting bundles into the band, and averaged 6 minutes per run. On the latest code (round 7), Gemma 4 passes 16 of 18 single-item runs and 2 of 2 bid-package runs; the two failures kept a Prado-and-Hilux bundle in the band with the right verdict (EVALS.md). With thinking on, at temperature 0, Qwen3 once looped for over an hour before its first tool call. Any model that `langchain.init_chat_model` supports can be swapped in with `FAIRPRICE_MODEL`.

## Audit

Every call to FairPrice's server is audited by the server itself, whoever makes it: a row in the `audit_log` table with inputs, outputs, timing, run id, organisation and caller. The CLI logs its calls to the borrowed DuckDB server the same way. Human decisions (including the approver's name) and run starts and finishes are logged the same way, and the run's own steps are in `run_steps`. `uv run fairprice --audit <run id>` prints a run's log in time order.

The `runs` and `run_steps` tables hold every review and each step it recorded. The `reports` table holds one row per report: its name, run id, format, files, approver, dates, and the report itself as JSON (summary, prices and sources). The report files are the deliverable a committee opens; the table is the record that tools read.

These tables are written through SQLAlchemy (`db.py`), so `FAIRPRICE_SQL_URL` can point them at Postgres, MySQL or another database instead of the default SQLite file. We have tested it on SQLite only. LangGraph's run state (used by `--resume`) stays in SQLite. LangGraph has a Postgres checkpointer if that ever needs to move too.

## Report storage

`storage.py` keeps reports on the local disk by default, or in S3-compatible object storage (`FAIRPRICE_STORAGE=s3`) when several servers need to share them or reports need backing up with the rest of an organisation's files. Disk space is not the reason: a report is 10 to 20 KB. The intended deployment is MinIO on the organisation's own servers. Reports can contain bidders' prices from an evaluation in progress, which are commercially confidential. Keep them inside your own organisation and jurisdiction: on this computer (the default), or in object storage you run yourself, such as MinIO on your own servers. Use a cloud provider only if your procurement and data rules allow it. Both providers have the same four operations (save, load, delete, list), and report names cannot contain folders, so a name can never point outside the store. The draft, approval and retrieval cycle is tested on both providers in the eval suite, using a mocked S3 bucket (`moto`) for S3.
