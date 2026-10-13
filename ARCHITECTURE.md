# Architecture

![FairPrice architecture](docs/fairprice-architecture.png)

## Agent shape

A LangGraph state graph with six nodes. Run state is checkpointed in SQLite (`data/runs.sqlite`), so a run paused at the approval gate can be resumed later with `--resume <run id>`.

| Node | What it does | Model? |
|---|---|---|
| **Analyst** | Searches past awards, decides which records describe the same product, rejects bundles, lot prices and different specifications with a reason, and retries with broader keywords when too few remain. | Yes |
| **Self-check** (inside Analyst) | Sends the Analyst back if it ran a search it never price-checked, or stopped with too few comparables before four searches. Then merges every record it kept across all checks into one final check. | No |
| **Challenger** | A second agent, with different tools, that tries to disprove the band using its own SQL. It names records to remove and gives a verdict: STANDS or DISPUTED. | Yes |
| **Recheck** | Rebuilds the band without the records the Challenger removed. | No |
| **Draft** | Builds the report (`report.py`) from tool outputs only: first as data, stored as a draft row in the `reports` table, then as files in the chosen format (xlsx by default, csv or json) in the report store, named `DRAFT-…`. It cites past purchases by the publisher's own record ID (OCID and contract ID), not FairPrice's internal row numbers, which change when the data is re-downloaded. The model writes no numbers. | No |
| **Approval gate** | `interrupt()`: a named officer picks approve, revise (with questions, which restarts the Analyst) or reject from a menu. Revise and reject delete the draft. | Human |
| **Save** | Calls `write_price_review(report, approval)` with the approval the person granted at the gate; the tool approves the stored report under that person's name and writes its final files. | No |

The model judges; code counts. Every number in a report comes from a tool output, and every past purchase is cited by its official record ID.

### Bid packages (`--package`)

`batch.py` reads a bid package (xlsx, csv or json) into one form and checks it against `bid_package.schema.json` and the rules the schema cannot express (unique BoQ lines and bidders, every bid line in the BoQ). A package that fails is refused with each problem's location; nothing is interpreted. Each row keeps its location (sheet and row, csv row, or JSON path) so findings can cite it. It runs the same graph once per distinct item, paused after Recheck, so it reuses the same evidence and the same evals. Each run is saved under its own id, so an interrupted batch resumes from the lines that finished. One failed line is reported and does not stop the batch.

Code then does three things:

- compares every bidder's unit price with that item's band;
- checks the bid files: arithmetic errors, missing lines, quantities that differ from the BoQ, and identical prices across bidders;
- builds one draft report for the package.

There is one approval gate for the whole package, and `revise` can redo chosen lines. On approval, `write_price_review` approves it as for a single item. The output never totals or ranks bidders. The Analyst is shown the median quoted price, not any one bid, so no single bidder steers which records it picks.

## MCP servers

| Server | Built or borrowed | Tools and resources | Used by |
|---|---|---|---|
| `fairprice-mcp` (`src/fairprice/server.py`) | Built, with the official MCP Python SDK | `find_comparables`, `check_price_comparability`, `write_price_review` (the action tool), `get_price_review` (lists approved reports or returns one), `list_countries` and `add_country` (load a country's data in the background), resource `ocds://line/{id}` | Analyst, Recheck, Save, other MCP clients |
| `mcp-server-motherduck==1.1.0` | Borrowed (MotherDuck, open source) | `execute_query`, `list_tables`, `list_columns`, `list_databases`; read-only by default for local files | Challenger |

**Why borrow the DuckDB server:** it is a maintained SQL server with a read-only mode, row limits and query timeouts already built in. Writing our own would add risk and no value. Keeping it separate also means the Challenger cannot simply rerun the Analyst's tools to confirm the Analyst's conclusion. It needs MCP SDK 2.x while our LangChain adapter needs 1.x, so it runs in its own environment through `uvx`. Once it is cached it starts with `--offline`, because a network blip during one eval run once failed the run when `uvx` checked PyPI.

**Human approval in the tools themselves.** `write_price_review(report, approval)` and `add_country(code, approval)` are the two tools that change something, and neither runs on an AI's say-so. Without an `approval`, each records a pending request in the `approvals` table and replies with `fairprice --approve <id>`, which a person runs in the terminal: it shows what is asked, takes the person's decision and name, and carries the action out. In FairPrice's own runs, choosing approve at the gate menu grants the approval. An approval is single-use and tied to one action and one target (a report name or a country code), and the approver's name comes from the approval, never from the caller. `write_price_review` also reads the records the report cites **from the stored report itself**, so a caller cannot approve a report with a different list of sources, and it refuses a report that cites no records or records that do not exist. It then marks the row approved, writes the final files from the stored data in the report's format, and deletes the draft files. `get_price_review` reads the same table and returns the report as JSON, whatever its file format and wherever its files are stored.

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

The brief says the data will not be cleaned for us. FairPrice uses it as published and handles each problem in code, with a reason the committee can read.

| Problem | How much, in our data | What FairPrice does |
|---|---|---|
| Missing quantities | All 109,237 Kenya awards; 4,518 of 17,044 Nigeria contracts | No unit price is computed. The record is flagged and excluded, never estimated. Tested by `kenya-no-quantities`. |
| Missing or placeholder dates | 1,698 Nigeria and 14,911 Kenya records have no date; NOCOPO also uses 2001-01-01 as a placeholder | Flagged `invalid_date` and excluded, because inflation cannot be adjusted without a year. Tested by `placeholder-dates`. |
| Extreme values: lot prices, bundles, duplicates | Common, e.g. "supply of 2 vehicles" with quantity 1 | Flags, duplicate detection, and a band of median and quartiles, which a few extreme records do not move. The Analyst rejects bundles with a reason; the Challenger looks for the ones it missed. |
| Multi-item contracts | Nigeria contracts with several items | No unit price can be assigned to one item, so they are flagged `multi_item_contract` and excluded. |
| Prices from different years | Nigerian consumer prices rose more than 5x between 2015 and 2025 | Every price is adjusted with World Bank CPI to the year under review. If that year is not published yet, the latest year is used and the report says so. |
| Test entries published as real awards | 180 Nigeria records from buyer "TEST MINISTRY - NOCOPO", 135 with a unit price | Flagged `test_record` and excluded. Found when a test entry appeared in a laptop band. |
| Missing buyer names | 9 Nigeria records | Shown blank. Nothing depends on the buyer name. |
| Broken supplier identifiers | Present | Not used: reports never name suppliers, so a broken identifier cannot produce a wrong flag against a company. |
| Products with no history | e.g. 2 published MRI purchases | `insufficient_comparables`, never a band padded with other products. Tested by `no-benchmark`. |
| Errors in bid files | Arithmetic, missing lines, changed quantities, unreadable cells | Flagged with sheet and row. A failed line does not stop the batch. Tested by the model-free bid file check. |

## Models

The default is **Gemma 4** (open weights, 8B) through Ollama, run locally with thinking mode off and a 4,096-token output cap. We started with Qwen3 8B, as planned, but switched after the evals. On the same code, Gemma 4 passed 17 of 18 single-item runs at about 2 minutes each. Qwen3 8B passed 12 of 18, kept letting bundles into the band, and averaged 6 minutes per run. On the latest code (round 7), Gemma 4 passes 16 of 18 single-item runs and 2 of 2 bid-package runs; the two failures kept a Prado-and-Hilux bundle in the band with the right verdict (EVALS.md). With thinking on, at temperature 0, Qwen3 once looped for over an hour before its first tool call. Any model that `langchain.init_chat_model` supports can be swapped in with `FAIRPRICE_MODEL`.

## Audit

Every MCP tool call on both servers passes through one interceptor, which prints it on screen and adds a row to the `audit_log` table with inputs, outputs, timing and run id. Human decisions (including the approver's name), drafts created and drafts deleted are logged the same way. `uv run fairprice --audit <run id>` prints a run's log in time order.

The `reports` table holds one row per report: its name, run id, status (draft or approved), format, files, approver, dates, and the report itself as JSON (summary, prices and sources). The report files are the deliverable a committee opens; the table is the record that tools read.

Both tables are written through SQLAlchemy (`db.py`), so `FAIRPRICE_SQL_URL` can point them at Postgres, MySQL or another database instead of the default SQLite file. We have tested it on SQLite only. LangGraph's run state (used by `--resume`) stays in SQLite. LangGraph has a Postgres checkpointer if that ever needs to move too.

## Report storage

`storage.py` keeps reports on the local disk by default, or in S3-compatible object storage (`FAIRPRICE_STORAGE=s3`) when several servers need to share them or reports need backing up with the rest of an organisation's files. Disk space is not the reason: a report is 10 to 20 KB. The intended deployment is MinIO on the organisation's own servers. Reports can contain bidders' prices from an evaluation in progress, which are commercially confidential. Keep them inside your own organisation and jurisdiction: on this computer (the default), or in object storage you run yourself, such as MinIO on your own servers. Use a cloud provider only if your procurement and data rules allow it. Both providers have the same four operations (save, load, delete, list), and report names cannot contain folders, so a name can never point outside the store. The draft, approval and retrieval cycle is tested on both providers in the eval suite, using a mocked S3 bucket (`moto`) for S3.
