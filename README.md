# FairPrice

An MCP server, with its own agent, that prepares sourced evidence for a public tender evaluation committee. Give it one item and a price, or a whole tender: its bill of quantities, its required documents and every bidder's price schedule. It:

1. ticks off each bidder's documents: missing, expired, or missing years;
2. checks each bid file for arithmetic errors, missing lines and changed quantities;
3. for each line, finds comparable past awards in published Open Contracting data, rejects records that are not truly comparable, adjusts prices for inflation, and has a second agent try to disprove the result;
4. drafts a report in which every number comes from a tool and every record is cited, and waits for a named officer to approve it.

**The product is the MCP server** (`fairprice-server`). It runs on one computer for one person, or as a web server that procurement systems and other AI apps connect to with access keys, each organisation seeing only its own reviews. The `fairprice` command line is one client of it.

**FairPrice never totals, ranks or recommends bidders, and never decides an award.** The committee does.

Built for the Agentic AI for Africa Innovation Challenge, Governance track (theme: Financial services, value for money).

## Set up

Tested on macOS (Apple M5, 16 GB RAM). You need:

- about 12 GB of free disk space (10 GB for the model, 2 GB for the database);
- 16 GB of RAM;
- an internet connection for setup and the first run only.

### 1. Install uv (the Python package manager)

uv installs the right Python version (3.12) and every package for you. You do not need to install Python yourself.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal, then check it works: `uv --version`.

### 2. Install Ollama and download the model

Ollama runs the AI model on your own computer, so no data leaves it.

- **macOS / Windows:** download and install from https://ollama.com/download, then open the Ollama app. It keeps running in the background.
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`

Then download the model once (about 10 GB):

```bash
ollama pull gemma4
```

Check that Ollama is running and has the model: `ollama list` should show `gemma4:latest`.

**Ollama on another machine.** If your computer cannot run the model (it needs about 10 GB of memory), run Ollama on a machine that can, such as a server with a GPU, and point FairPrice at it with `OLLAMA_HOST`, the same setting Ollama's own tools use:

```bash
OLLAMA_HOST=http://gpu-server:11434      # put this line in .env (see step 4), or export it
```

On that machine, start Ollama so other computers can reach it (`OLLAMA_HOST=0.0.0.0 ollama serve`) and run `ollama pull gemma4` there. Keep it on your own network: bid prices are sent to the model.

**Another model.** Set `FAIRPRICE_MODEL`, for example `FAIRPRICE_MODEL=ollama:qwen3:8b` (after `ollama pull qwen3:8b`). The evals in EVALS.md were measured with `gemma4`; other models will behave differently.

**Other ways to run an open-weights model.** Instead of Ollama, FairPrice can use any server on your own computer or network that offers an OpenAI-compatible API: LM Studio, llama.cpp's `llama-server`, Apple's `mlx_lm.server`, Jan, LocalAI, vLLM, SGLang, Hugging Face TGI or NVIDIA NIM.

```bash
uv sync --extra openai-compatible                       # once
FAIRPRICE_MODEL=openai:<the model name the server uses>
FAIRPRICE_MODEL_URL=http://localhost:1234/v1             # the server's address (this one is LM Studio's default)
FAIRPRICE_MODEL_API_KEY=...                              # only if the server was started with a key
```

Three things to set on the server itself:

- **Tool calling must be on.** FairPrice's agent works by calling tools. vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser for the model>`; llama.cpp needs `--jinja`; in LM Studio, pick a model marked for tool use.
- **A context window of at least 16,000 tokens.** With Ollama, FairPrice sets this itself; on these servers it is a server option (for example `-c 16384` for llama.cpp, `--max-model-len 16384` for vLLM, the context-length setting in LM Studio).
- **Thinking mode off**, for models that have one, where the server allows it.

`uv run fairprice --check` (and every run) checks that the server is reachable, serves that model, and actually calls tools: it sends one short test request and, if the model answers in text instead, says which setting to change. FairPrice refuses `openai:` models without `FAIRPRICE_MODEL_URL`, so it never sends procurement data to a hosted API by default.

`--check` also sends one long test prompt to see whether the server reads it whole; a server that silently cuts long conversations makes the agent lose its own search results and repeat the same search.

**What we tested.** We ran this route against Ollama's own OpenAI-compatible endpoint (`FAIRPRICE_MODEL_URL=http://localhost:11434/v1`), which serves the same Gemma 4:

- With Ollama's default context window, the server cut each conversation to about 800 tokens, and Gemma repeated the same search 15 times without ever checking a price. That is why `--check` now tests the context window.
- With a 16,384-token window (a Modelfile with `PARAMETER num_ctx 16384`), the run worked: it searched, built a band and reached the approval gate in 5 model calls. But this route cannot switch Gemma's thinking off, so it wrote about 6,900 output tokens instead of about 1,000 and took 6 minutes instead of under 2.

The evals in EVALS.md were all run on the native Ollama route, not this one. Run `--check` and a short eval (`uv run fairprice-evals --cases hilux-above,no-benchmark --repeats 1`) before relying on a new server.

### 3. Get the code

```bash
git clone https://github.com/iamTMTY/fairprice.git
cd fairprice
```

(No git? Download the ZIP from the repository page, unzip it, and `cd` into the folder.)

### 4. Check the setup

```bash
cp .env.example .env     # optional: every setting has a default; edit it to change the model or OLLAMA_HOST
uv run fairprice --check
```

```
✓ Model ollama:gemma4 at http://localhost:11434
• Procurement data: not built yet; the first run downloads about 60 MB, or run `uv run fairprice-ingest`
• Borrowed DuckDB MCP server: downloads on the first run (needs internet once)
✓ Reports: local storage, xlsx files
✓ Database for runs, reports and the audit log: reachable
Ready.
```

Anything marked ✗ comes with the fix, for example `Ollama is not reachable at http://localhost:11434 … Install it from https://ollama.com/download and open it, or set OLLAMA_HOST to where it runs` or `Run ollama pull gemma4`. Every run makes the same check first and stops with the same message instead of failing halfway.

### 5. Run it

```bash
uv run fairprice "Toyota Hilux double cabin pickup" --unit-price 120000000 --year 2025
```

That is the one command. The first time, it also:

1. creates a local Python environment in `.venv/` and installs the packages (under a minute);
2. downloads about 60 MB of public procurement data (every Nigerian and Kenyan publication) and builds a local database in `data/` (a few minutes).

After that, everything runs offline. Each item takes about 2 minutes on the laptop above.

**Cost of a run.** Every run ends with what it cost, for example:

```
Cost of this run: 9 model calls, 41,203 input and 1,876 output tokens, 98 s of model time, API fees USD 0.00 (local model).
```

The same figures are stored with the report (in its JSON, under `report.cost`) and in the audit log (`uv run fairprice --audit <run id>`). With a local model there are no API fees. If you point FairPrice at a paid provider, set its prices (`FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS`, `FAIRPRICE_PRICE_PER_MILLION_OUTPUT_TOKENS`, `FAIRPRICE_PRICE_CURRENCY`) and the fee is worked out per run. EVALS.md gives measured tokens per run for each eval task.

At the end, a short summary is printed with the draft report's location; open it in Excel, Numbers or LibreOffice (or set `FAIRPRICE_OPEN_DRAFT=1` to open it automatically). It is saved as `reports/DRAFT-<item>-<run id>.xlsx` and marked "DRAFT – not approved". Then you pick a decision with the arrow keys and Enter, or by pressing 1, 2 or 3:

- `approve`: you are asked for your full name. The final report is written with "Approved by <your name> on <date>" in place of "DRAFT – not approved", under a name without "DRAFT", and the draft is deleted;
- `revise`: you say what to investigate. The draft is deleted, the agent runs again, and a new draft opens;
- `reject`: the draft is deleted and nothing is kept.

The report has two tables:

- **Prices:** one row per line and bidder, with the quoted price, the typical past price, how many times typical it is, the result, the sources it rests on, and any problems found in that bid;
- **Sources:** every past purchase the report relies on, numbered, with the buyer, year, what was bought, price paid, price in today's money, and the official record ID on the national portal (OCID and contract ID).

Choose the file format with `--format` or `FAIRPRICE_REPORT_FORMAT`:

| Format | Files | Approval shown |
|---|---|---|
| `xlsx` (default) | One workbook with a Prices sheet (results colour-coded) and a Sources sheet | One line under each table and in the print header |
| `csv` | `…-prices.csv` and `…-sources.csv`; they open in Excel, Numbers or LibreOffice | A Status column on every row, so the files stay clean tables for other programs |
| `json` | One `….json` file with the report, prices and sources | `status`, `approved_by` and `approved_at` fields |

Whatever the format, the report is also kept as data in the `reports` table of `data/runs.sqlite`, and the MCP tool `get_price_review` returns it as JSON.

## Review a whole tender

One workbook holds the whole tender, and one run does the whole job: tick off each bidder's documents, check each bid file, compare every price with past purchases, and write one report for a person to approve.

```bash
uv run fairprice --template tender.xlsx                     # a blank workbook with every sheet (or .json, or .csv)
uv run fairprice --package tender.xlsx --year 2025          # read, check, compare, draft the report, approve
uv run fairprice --package examples/bid-package.xlsx --year 2025   # try it on the example
```

| Sheet | What goes in it |
|---|---|
| **BoQ** | The bill of quantities: Item No, Description, Unit, Quantity, and optionally Budget per unit |
| **Documents** | The documents the tender requires, one row each (Ref, Document, Rule), and one column per bidder for the committee's ticks |
| **One sheet per bidder**, named after the bidder | That bidder's price schedule: Item No, Unit Price, and optionally Description, Unit, Quantity, Total Price |

`examples/bid-package.xlsx`, `.csv` and `.json` are the same **synthetic** tender: three invented bidders, invented prices, and seven planted problems (four in the price schedules, three in the documents). All forms must match one schema, [`src/fairprice/bid_package.schema.json`](src/fairprice/bid_package.schema.json), which another system can use to check a file before sending it. A json package holds the same three parts: `{"boq": [...], "documents": [...], "bids": [...]}`. A csv file holds one table, so it carries the BoQ and the price schedules (rows whose Bidder is `BoQ` are the bill of quantities) but no documents checklist; use xlsx or json for that.

### Ticking off documents

Every tender lists the documents a bid must include (the form of tender, the signed price schedule, a tender security, tax and registration certificates…); a bid missing one is rejected before its prices are looked at. Copy the tender's list into the Documents sheet and tick each bidder:

| Ref | Document | Rule | Bidder A | Bidder B | Bidder C |
|---|---|---|---|---|---|
| 1 | Form of Tender, signed and stamped | Submitted | Yes | Yes | Yes |
| 2 | Tender security | Submitted | Yes | Yes | No |
| 3 | Tax compliance certificate | Valid on 2025-10-15 | 2026-06-30 | 2025-09-30 | 2026-03-31 |
| 4 | Tax clearance certificate | Covers 2022-2024 | 2022-2024 | 2022-2024 | 2023-2024 |

| Rule | What a bidder's cell holds | What FairPrice reports |
|---|---|---|
| `Submitted` | Yes or No | "Bidder C did not submit document 2: Tender security" |
| `Valid on YYYY-MM-DD` (usually the closing date) | The date the document is valid until (an Excel date or YYYY-MM-DD), or No | "Tax compliance certificate is valid until 2025-09-30, but the tender requires it to be valid on 2025-10-15" |
| `Covers YYYY-YYYY` | The years it covers (such as 2022-2024), or No | "covers 2023-2024, but the tender requires 2022–2024 (missing 2022)" |

The report shows each bidder's documents as "All required documents" or "N document problems", lists every problem with its cell, and adds a Documents sheet with the checklist and each result. **FairPrice checks documents as the committee recorded them; it cannot tell whether a certificate is genuine.** That needs the issuing body (a tax authority's certificate checker, the company registry), and the report does not claim otherwise.

### The columns

The price columns are those of a standard price schedule (Item No, Description, Unit, Quantity, Unit Price, Total Price), so a bidder's schedule can usually be pasted in as it is. The headings procuring entities commonly use for the same columns are accepted too:

| Field | Accepted headings (any case; notes in brackets and a currency such as "in KES" are ignored) | Where |
|---|---|---|
| line | Item No, Item Number | BoQ and bids (required) |
| item | Description, Description of Goods | BoQ (required), bids (optional) |
| unit | Unit, Unit of Issue, Unit of Measure | optional |
| quantity | Quantity, Qty | BoQ (required), bids (optional) |
| unit_price | Unit Price, Rate, Unit Rate | bids (required) |
| total | Total Price, Total Amount, Total | bids (optional) |
| country_of_origin | Country of Origin | bids (optional) |
| budget | Budget per unit | BoQ (optional): the procuring entity's budget per unit, shown in the report against each quote |
| bidder | Bidder, Tenderer | csv only |

So KRA's "Rate in KES (VAT inclusive)" and "Total Amount (KES)" read as the unit price and the total.

**FairPrice does not interpret a package; it checks it.** Every column must have one of the headings above, and only one column may have each meaning. Prices must be plain numbers (`2500000`, `2,500,000` or `2500000.50`). Every bid line must be a line in the BoQ. A document rule must be one of the three, and every cell must fit its rule. A package that breaks any rule is refused before any analysis, with each problem and where it is, for example:

```
- sheet “Bidder B”, row 1: unknown column(s) Price per piece; the columns are Item No, Description, Unit, Quantity, Unit Price, Total Price, Country of Origin
- bid-package.csv, row 7, Unit Price: 'NGN 2,500,000' is not a number
- sheet “Documents”, row 4, Bidder A: 'Yes'; write the date it is valid until (YYYY-MM-DD), or No
```

Problems are reported in two passes: cells that cannot be read first, then rules such as lines missing from the BoQ. A bid with mistakes in its numbers (totals that do not add up, missing lines, changed quantities) or missing documents is read; those are findings for the committee, not reasons to refuse the file.

Before the agent starts, FairPrice prints what it read, so a wrong file is caught in seconds:

```
Read bid-package.xlsx: BoQ with 3 lines, 3 bidders, 4 required documents
  Bidder A: 3 of 3 lines priced
  Bidder C: 2 of 3 lines priced (not priced: 3)
```

**What you get:** one draft report for the whole tender, in your chosen format. Each bidder's documents result; each quote against past prices and, if the BoQ has budgets, against the budget ("Times the budget"); and each bidder's problems in plain sentences that name the sheet and row: totals that do not add up, missing lines, quantities that differ from the bill of quantities, bidders quoting identical prices on most lines, and missing, expired or incomplete documents. Findings state facts, not law: what a finding means (an arithmetic error is corrected in some countries' rules and disqualifying in others) is for the committee under its own rules. Bidders appear in the order the file lists them, and are never totalled, ranked or recommended.

**Why the price comparison is a step in the procedure, not an extra.** Kenya's PPRA standard tender document tells the procuring entity what to do with an abnormally high tender (ITT 36): "make a survey of the market prices" and "check if the estimated cost of the contract is correct". In Nigeria, BPP's Price Intelligence Unit reviews project costs against benchmark unit rates before approval. FairPrice does that survey from published past awards, with every price sourced; with a budget in the BoQ, it also shows whether the budget itself is in line with past prices.

You approve, revise or reject the package once. On `revise`, you also choose which lines to redo.

The agent runs once per distinct item, not once per bidder, so three bidders take about as long as one. A 40-line BoQ takes about 80 minutes on the laptop above.

### Designed, not built yet: reading bid files in any layout

Real bids still arrive in layouts the headings above do not cover: title rows above the headings, merged cells, section rows ("Glazing", "Gypsum boards"), VAT and subtotal rows. Today a committee copies such a schedule into the template by hand. That is deliberate: a person, not FairPrice, decides what an unfamiliar column means.

The next step we designed is to let the model propose the mapping, never the values:

1. Gemma reads the first rows of each sheet and proposes its structure: which row holds the headings, which column is which field, which rows to skip (subtotals, VAT), and whether the sheet is the BoQ or a bid.
2. Code applies the mapping and reads every number itself, then checks the mapping: the schema; quantity × unit price = total on most rows (if not, the columns are probably swapped); bid lines match the BoQ. A failed check gets one retry with the error, then the file is refused.
3. A person confirms the mapping and what was read before any analysis, and the mapping is logged in the audit log.

We did not build it for this submission. Bids are not published anywhere, so we could only test it on messy files we wrote ourselves, and a test set we design to pass is not evidence. The strict format above is the safe path until it can be tested on real bid files.

## Other commands

```bash
uv run fairprice --resume <run id>                       # continue a run paused at the approval gate
uv run fairprice --package file.xlsx --resume <batch id>  # rerun a package, reusing the lines that finished
uv run pytest --cov                                      # unit and integration tests, with coverage (about 40 s, no model needed)
uv run fairprice-evals --repeats 2                       # run the eval suite (see EVALS.md); about an hour
uv run fairprice-evals --cases bid-package --repeats 1   # run one eval case
uv run fairprice-ingest                                  # rebuild the database from the loaded publications
FAIRPRICE_MODEL=ollama:qwen3:8b uv run fairprice ...     # use another model (ollama pull qwen3:8b first)
uv run fairprice --audit <run or batch id>               # print a run's audit log: every tool call and decision
uv run fairprice --approve [request id]                  # decide requests from AI apps to approve a report or add a country
```

## Configuration for a server

The defaults need no setup: reports go to `reports/`, and the audit log and run state go to `data/runs.sqlite`. If several servers need to share reports and the audit log, you can keep reports in object storage and the audit log in a shared database instead:

Put settings in a `.env` file next to `pyproject.toml` (copy `.env.example`, which lists them all), or set them in the environment, which wins over `.env`. `.env` is never committed.

| Setting | Default | What it does |
|---|---|---|
| `FAIRPRICE_STORAGE` | `file` | `s3` keeps reports in S3-compatible object storage: self-hosted MinIO on your own servers, or a cloud provider your rules allow. Run `uv sync --extra s3` once. |
| `FAIRPRICE_S3_BUCKET`, `FAIRPRICE_S3_PREFIX` | none, `fairprice/reports/` | The bucket and folder for reports. Credentials come from the standard `AWS_*` settings, which every S3-compatible store uses, not only AWS. Set `AWS_ENDPOINT_URL` to your MinIO server or another provider's endpoint. |
| `FAIRPRICE_REPORTS` | `reports/` | The folder for reports when `FAIRPRICE_STORAGE=file`. |
| `FAIRPRICE_SQL_URL` | `sqlite:///data/runs.sqlite` | Where the audit log is kept, as a SQLAlchemy URL, e.g. `postgresql+psycopg://user:password@host/fairprice` (install that database's driver). Tested on SQLite only. Run state for `--resume` stays in `data/runs.sqlite`. |
| `FAIRPRICE_REPORT_FORMAT` | `xlsx` | The report file format: `xlsx`, `csv` or `json`. `--format` overrides it for one run. |
| `FAIRPRICE_OPEN_DRAFT` | `0` | Set to `1` to open the draft report automatically in Excel or Numbers. |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama runs, if not on this computer. |
| `FAIRPRICE_MODEL` | `ollama:gemma4` | The model, as `provider:model`. |
| `FAIRPRICE_MODEL_URL`, `FAIRPRICE_MODEL_API_KEY` | none | Your OpenAI-compatible server, for `FAIRPRICE_MODEL=openai:<name>` (see step 2). |
| `FAIRPRICE_PRICE_PER_MILLION_INPUT_TOKENS`, `…_OUTPUT_TOKENS`, `FAIRPRICE_PRICE_CURRENCY` | `0`, `0`, `USD` | Prices for working out API fees per run, if you use a paid provider. |

With S3 storage, the draft and approved reports are shown as download links that work for one hour.

**Where reports may go:** Reports can contain bidders' prices from an evaluation in progress, which are commercially confidential. Keep them inside your own organisation and jurisdiction: on this computer (the default), or in object storage you run yourself, such as MinIO on your own servers. Use a cloud provider only if your procurement and data rules allow it.

## The MCP server: connect other tools

### Tools

| Tool | What it does |
|---|---|
| `start_review(item, unit_price, currency, year, country)` | Starts a review of one price; returns its `run_id`. The currency defaults to the country's. |
| `start_package_review(package \| file + filename, title, currency, year, country)` | Starts a review of a whole tender: the package as JSON, **or the workbook itself** (`file` is its contents in base64, `filename` ends in .xlsx, .csv or .json). Documents and bid files are checked at once; problems come back with their sheet and row. |
| `plan_searches(run_id, searches, notes, line)` | Records the search plan for a line |
| `find_comparables(run_id, keywords, line, year_from, year_to, limit)` | Searches past awards in the review's country; every word must match, in any order |
| `check_price_comparability(run_id, candidate_ids, rejected, line)` | The band and the verdict. Only records the review's own searches found count; the price, currency and year come from the review. |
| `challenge_review(run_id, verdict, reason, objections, line)` | Records a second, independent check of a line |
| `review_with_fairprice(run_id)` | **For apps that do not reason themselves** (a procurement portal): FairPrice's own agent reviews every line in the background and drafts the report |
| `review_status(run_id)` | Progress of that review; when done, the summary, the draft and the approval request |
| `write_price_review(run_id, approval, format)` | Without approval: the draft report and a request for a person. With a person's approval: the final report. `format`: xlsx, csv or json. |
| `get_price_review(run_id)` | An approved report as JSON, with links to its files; without a run_id, the list |
| `list_countries()`, `add_country(code, approval)` | The countries with data, and loading one (needs a person's approval) |

| Resource | What it is |
|---|---|
| `fairprice://bid-package/schema` | The tender format (JSON Schema): BoQ, documents, bids |
| `fairprice://bid-package/example` | A complete example tender |
| `fairprice://bid-package/template.xlsx` | A blank tender workbook (also at `/template.xlsx` on a web server) |
| `ocds://line/{id}` | The published record behind a cited source |

An app either does the reasoning itself (plan, search, check, challenge, then `write_price_review`), or hands the run to `review_with_fairprice` and follows `review_status`. Either way **it never sends a report, a price or a source list to be saved**: reports are built only from what the review's own tool calls recorded.

### On one computer (stdio)

```bash
uv run --directory /path/to/FairPrice fairprice-server
```

Any MCP client (Claude Desktop, an IDE, your own app) can start it this way. There are no keys: everything belongs to this computer.

### As a web server (HTTP)

```bash
export FAIRPRICE_PUBLIC_URL=https://fairprice.example.org      # where people and apps reach it
export FAIRPRICE_SECRET=<a long random string>                   # signs report download links
uv run fairprice-server --add-key ministry-of-works --role app --name "e-GP portal"
uv run fairprice-server --add-key ministry-of-works --role approver --name "Ada Obi"
uv run fairprice-server --http --host 0.0.0.0 --port 8000
```

- **MCP at `/mcp`** (streamable HTTP). Every call needs `Authorization: Bearer <app key>`. A key belongs to one organisation, and a caller sees only its own organisation's reviews, reports and approval requests.
- **Two kinds of key.** An *app* key calls the tools. An *approver* key belongs to a named officer and can only approve. **So an AI app's key can never approve anything, and the name on an approved report is always the officer's**, taken from their key.
- **Approvals at `/approve`**: the officer enters their key and the request, sees what is asked with links to the draft, and approves or rejects. Software they use (FairPrice's CLI) can do the same through `/api/approvals`.
- **Report files** come back as signed links that work for one hour (or S3's own links with `FAIRPRICE_STORAGE=s3`).
- **Every tool call is audited by the server**, whoever makes it: inputs, output, time, organisation and caller, in the `audit_log` table. Human decisions are logged with the officer's name.
- Keys are stored as hashes; the secret is shown once. Revoke one with `--revoke-key <id>`. Put the server behind HTTPS (a reverse proxy) when it leaves your own network. For several servers, point `FAIRPRICE_SQL_URL` at Postgres.
- **Bid prices are confidential.** Run the server inside the procuring organisation or its government's own data centre.

### The CLI as a client of a web server

```bash
export FAIRPRICE_SERVER_URL=https://fairprice.example.org FAIRPRICE_API_KEY=<app key>
uv run fairprice --package tender.xlsx --year 2025     # uploads the workbook; the server's agent reviews it
FAIRPRICE_APPROVER_KEY=<approver key> uv run fairprice --approve <request>
```

### Approving on one computer

**An AI app can never approve a report or add a country by itself.** On one computer, a person decides in the terminal:

```
$ uv run fairprice --approve              # list the requests waiting
$ uv run fairprice --approve 3f9a1c2b7d10e4a5
An AI app asks: Approve the price review 36daf9b9af36: laptop computer, 1 prices, 12 sources.
Decision (↑/↓ and Enter, or press a number)
❯ 1. approve  carry it out now, under your name
  2. reject   refuse it
```

Each approval works once, for that version of that review (if the review changes after the draft, the app must draft again) or that country, and is recorded in the `approvals` table and the audit log. Inside FairPrice's own runs, the approval menu at the end of a run is that person.

## If something goes wrong

| Message | Fix |
|---|---|
| `Ollama is not reachable at …` | Ollama is not running, or runs elsewhere. Open the Ollama app, run `ollama serve`, or set `OLLAMA_HOST`. `uv run fairprice --check` shows what is wrong. |
| `model "gemma4" not found` | Run `ollama pull gemma4`. |
| The first run stops while downloading or building | Run the same command again. Finished downloads are kept; an unfinished build starts over. |
| `… was not read: it does not match the bid package format` | Fix each listed problem; the templates in `examples/` show the exact layout. |
| A run is very slow | About 2 minutes per item is normal on a laptop. Keep the computer awake and plugged in. |

## What is in the repo

| Path | What it is |
|---|---|
| `src/fairprice/server.py` | Our MCP server: 6 tools and 1 resource |
| `src/fairprice/storage.py` | Where reports are kept: local folder (default) or self-hosted S3-compatible storage |
| `src/fairprice/agent.py` | The LangGraph agent, approval gate, audit log and CLI |
| `src/fairprice/batch.py` | Bid packages: reading xlsx, csv and json against the schema, and the bid file checks |
| `src/fairprice/bid_package.schema.json` | The bid package format (JSON Schema) |
| `src/fairprice/report.py` | The report: its data, its xlsx, csv and json files, its plain-language wording, and the terminal summary |
| `src/fairprice/db.py` | The SQL tables: the audit log and the reports |
| `src/fairprice/ingest.py` | Downloads and loads the OCDS and World Bank data |
| `src/fairprice/evals.py`, `evals/cases.json` | The eval suite |
| `tests/` | Unit and integration tests (99% line and branch coverage); see below |
| `evals/classifier_experiment.py` | A model we tested and rejected (see EVALS.md) |
| `examples/bid-package.xlsx`, `.csv`, `.json` | The same synthetic bid package in each form; templates |
| `ARCHITECTURE.md` | Agent shape, MCP servers built and borrowed, and why |
| `EVALS.md` | Test tasks, pass and fail results, run-to-run variation, an unfixed failure |

Created when you run it (not in the repo): `data/` (downloads, database, run state), `reports/` (approved reports, and the draft awaiting a decision). `data/runs.sqlite` holds the audit log (the `audit_log` table: every tool call with inputs, outputs and timing, and every decision; read it with `--audit`) and the reports (the `reports` table).

## Tests

```bash
uv run pytest --cov
```

103 tests, 99% line and branch coverage, about 30 seconds, and no model or network needed. They run against a small synthetic dataset built through the real ingest (registry, downloads and World Bank mocked): a Nigeria-style publication with item quantities and the traps the agent must handle (a bundle, a placeholder date, a test record, a duplicate, a multi-item contract), a Kenya-style one with award totals only, and a Rwanda-style one with prices per item. A scripted model stands in for Gemma, so the agent graph, the approval gate, the CLI, the bid packages and the eval runner all run for real; one integration test starts both MCP servers as subprocesses over stdio, and the web tests call the server over HTTP with access keys from two organisations. The tests never touch `data/`, `reports/` or your database.

The tests prove the code does what it should. They do not measure how good the model's judgement is; that is what the evals are for (EVALS.md).

## Add a country

Out of the box, FairPrice loads Nigeria and Kenya. Any country with data on the [OCP Data Registry](https://data.open-contracting.org/en/search/) can be added by its ISO country code:

```bash
uv run fairprice-ingest --list       # countries with data, and which are loaded
uv run fairprice-ingest --add RW     # add Rwanda
uv run fairprice --country RW "laptop" --unit-price 900000 --year 2025
```

Prices are taken to be in the country's own currency (RWF for Rwanda, read from its published data). If a quote is in another currency, say so with `--currency`, for example `--currency USD`; past prices are then converted at the World Bank's exchange rates.

The same two actions are MCP tools on our server, `list_countries` and `add_country`, so an AI app can add a country. `add_country` runs in the background, because a large country takes minutes; `list_countries` shows its progress.

Adding a country loads **every publication it has**: Nigeria's federal BPP and 12 others (11 state portals and Budeshi), Kenya's PPRA and Makueni County. They are different purchases: state governments buy through their own portals. They are downloaded unchanged into one file per country (`data/raw/nigeria.jsonl.gz`), and each record keeps its own publisher, so a report says "Nigeria: Ebonyi State" for a state purchase. The ingest also reads each publication's currency from its data, and fetches the World Bank's inflation and exchange rates for the country. Then it rebuilds the database. Existing records keep their ids, because new publications are numbered after the ones already loaded. Large countries take a while: Rwanda is 344 MB and adds about 12 minutes.

Whether a country can give prices per item depends on what it publishes. FairPrice detects which of three kinds each publication is:

| What the publication contains | Example | What FairPrice can do |
|---|---|---|
| A unit price for each contract item | Rwanda | Prices per item, one record per item |
| A quantity for each contract item | Nigeria | Prices per item for single-item contracts (contract value ÷ quantity) |
| Award totals, no quantities | Kenya | Searches only; every price check ends in "No comparison possible" |

**So a Kenyan tender can be read and checked today, but not benchmarked against Kenyan past prices.** The documents and bid file checks work on any tender. Kenya's published awards carry no quantities, so they give no price per item. Until they do, a Kenyan package's prices can be compared with another loaded country's past awards (`--country NG --currency KES`, converted at World Bank exchange rates and adjusted for inflation), which is evidence of a different market and should be read as such.

## Data and its limits

- **Nigeria** (federal BPP and its states) and **Kenya** (PPRA and Makueni County) are loaded by default, and the evals use them. Other countries can be added as above.
- **We built on the real-data half.** The price benchmarks come from real published awards. Portals publish awards, not bids, so the bid packages in the examples and evals are synthetic. FairPrice reads price schedules, not full bid documents (PDFs, certificates).
- Data is used as published. The agent handles placeholder dates, lot prices, bundles and duplicates; it does not clean the source.
- No live tenders and no personal data. Reports show buying agencies, not supplier names.

## Honest status

- Works end to end on real data with Gemma 4 run locally (default), and with Qwen3 8B.
- Small open models still make mistakes, such as searching with keywords that are too broad. The self-check catches some of these; EVALS.md shows what remains.
- Nigeria and Kenya are loaded by default. Others can be added with `fairprice-ingest --add`; Rwanda has been tested this way, but the evals cover Nigeria and Kenya only.
- Item matching uses keywords, not a product classification. This is the biggest limit on accuracy, and long BoQ descriptions make it worse.
- Tested on macOS only.

## Ideas we would test next

Not built. Each would be measured against the eval cases before replacing anything.

**Semantic search for comparable records.** Today the model guesses keywords and the search matches text literally, so "pickup" misses "pick-up", "laptop" misses "notebook computer", and a poor guess costs a retry. An open-weights embedding model run in Ollama (such as `nomic-embed-text` or `bge-m3`) could add a second search tool, `find_similar`, that finds records by meaning, next to the keyword search, which stays because it is precise when the words are right. We expect it to find the right records sooner on items with varied wording. We do not expect it to fix bundles or specifications: "2 Toyota Hilux and 1 Land Cruiser Prado" is very close in meaning to "Toyota Hilux", and embeddings barely tell "125cc" from "150cc". Nor would it make runs much faster: the search takes under a second, and nearly all the time is the model reasoning. Costs: a one-time indexing pass per country on the laptop (minutes for Nigeria and Kenya, longer for Rwanda), a few hundred MB of storage, and a full eval run to show it helps.

**A code rule for bundles.** Reject records whose description names another model of the same brand (a Hilux search that finds "Land Cruiser" or "Prado"). That is precise where embeddings are fuzzy, and it targets the failure in EVALS.md that we have not fixed.

**Reading bid files in any layout.** See "Designed, not built yet" under "Review a whole tender".

## Licence

MIT. Source data: OCP Data Registry publications 64 (Nigeria) and 147 (Kenya); World Bank WDI.
