# Northstar access intake

Use CPython 3.12 on Linux, from the repository root. The application uses only the standard library: SQLite, urllib, threading and POSIX file locks. `pylock.toml` locks an empty third-party dependency set; there is nothing to install. The supported command is `python3 -m access_intake`.

Reproduce the supplied mock bundle (20 messages plus one independently authored paraphrase):

```sh
python3 --version
env -u API_KEY python3 -m access_intake intake materials/requests --provider mock --fixtures tests/mock_responses --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake intake tests/inputs --provider mock --fixtures tests/mock_responses --state .state/intake.sqlite --output deliverables
env -u API_KEY python3 -m access_intake accuracy --expected tests/expected.json --actual deliverables/extractions.jsonl --output deliverables/accuracy.md
env -u API_KEY python3 -m unittest discover -s tests -v
```

Inspect current results, pending claims, exact evidence, source bytes decoded as UTF-8, processing settings and attempt history:

```sh
python3 -m access_intake queue --state .state/intake.sqlite
python3 -m access_intake show IT-001.json --state .state/intake.sqlite
python3 -m access_intake retry IT-001.json --state .state/intake.sqlite --output deliverables
python3 -m access_intake resume --state .state/intake.sqlite --output deliverables
python3 -m access_intake export --state .state/intake.sqlite --output deliverables
```

`intake` accepts one JSON file or a directory's immediate `*.json` files. Filenames are document identities across batches; a later file with the same name is a revision. Omitted documents remain. An unchanged successful source/configuration is reused. Failures remain visible and an intake rerun or `retry` makes a new attempt. Retry uses the latest registered bytes and recorded settings; it does not reread the original path. Resume processes pending or expired claims with their recorded settings. If application code or mock fixtures have changed since registration, attempts fail before any provider call; restore that revision or refresh with intake. Use intake with current files/options to refresh configuration instead.

The optional live provider is OpenAI Responses at `https://api.openai.com/v1/responses`, with `gpt-4.1-mini-2025-04-14`, temperature 0, 2400 maximum output tokens, strict JSON schema, `store=false`, and a 60-second HTTP timeout. The only application environment variable is `API_KEY`, loaded only for an OpenAI call. Set it through your environment/secret manager, then run:

```sh
python3 -m access_intake intake materials/requests --provider openai --model gpt-4.1-mini-2025-04-14 --temperature 0 --max-output-tokens 2400 --timeout 60 --workers 4 --state .state/live.sqlite --output .state/live-output
```

The model snapshot is listed in the [official model reference](https://developers.openai.com/api/docs/models/gpt-4.1-mini). The adapter follows the [official Responses request contract](https://developers.openai.com/api/reference/resources/responses/methods/create) and [Structured Outputs handling](https://developers.openai.com/api/docs/guides/structured-outputs). Its transport is tested with fake HTTP; no live model accuracy or account/model availability was observed. `--model`, `--temperature`, `--max-output-tokens`, `--timeout`, `--provider`, `--fixtures`, `--policy`, `--state`, `--output` and `--workers` configure processing. `--policy` supplies trusted policy text and an invalidation fingerprint; changing review semantics also requires updating the ordered router. Implementation/schema/prompt/policy/fixture hashes are recorded and invalidate reuse conservatively. No credential is persisted or included in errors/request evidence. `response_source` identifies mock or the actual response model (falling back to the configured model); `response_received=false` distinguishes failures before any response and labels that source as the attempted configuration.

Operators on one Linux host share the same SQLite path and output directory on a local filesystem. SQLite transactions serialize registration and claims. The last registered attempt for a filename wins, even if an older response finishes later; exported timestamps do not order revisions. A 300-second claim lease and 60-second heartbeat recover abandoned work. `resume` can be rerun after lease expiry; active claims are left to their owner. A crash after a provider call but before commit can repeat that call. Completed DB transactions survive restart. There is no exactly-once provider billing guarantee.

Four workers by default (1–32 configurable) allow ready messages to complete while a call is slow. Rate limits, timeouts, refusal and malformed replies become per-message errors with retry commands; there are no unbounded automatic retries. Queue/show read committed state during intake. Ctrl-C/termination can leave claims to expire; after recovery export rebuilds files from state. Each export publishes an immutable generation through an atomic `current` symlink; when reading both files, resolve `deliverables/current` once and read that directory. The root convenience links follow the current generation individually. Keep the state database and generations for history; no automatic pruning occurs.

All successful results await human review. A manager is a contact, never an approval. Prompt rules ask the model to ground facts in messages; local validation enforces schema, dates and exact quotes, and routing checks override patterns. These checks cannot prove arbitrary semantic extraction accuracy or detect every novel injection. Inspect evidence for each human decision.

The design accepts the stated 200–400 daily / 2,000 burst workload with bounded workers; no service-level completion deadline is promised. Tests verify mock workloads, shared-state claims, recovery and export coherence. Multi-host/network-filesystem SQLite, a live throughput benchmark, and live extraction accuracy are outside the performed verification. See `deliverables/verification.md`, `deliverables/accuracy.md`, and `planning/validation.md` for evidence and remaining submission steps.

Exit codes: 0 for a completed command (individual intake errors are in queue), 1 for accuracy differences, 2 for invalid arguments/paths, 130 for Ctrl-C. Resume does not automatically retry completed errors. Read each error's command or use `retry DOCUMENT_ID` explicitly.
