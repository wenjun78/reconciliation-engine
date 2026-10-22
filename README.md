# The Reconciliation Engine
## Data Engineer · AI-assisted take-home · Version 1.2

Build a local, recoverable settlement data product whose answers remain correct after late data, corrections, conflicting updates, retries and erasure requests. This is intentionally a demanding assessment of data engineering judgement and implementation.

**Claude, Codex and other coding assistants are allowed and expected.** You own the result. A fluent explanation without a working system will not pass.

### Time and equipment
- Timebox: **12 hours of active work**, submitted within seven calendar days. Stop at the timebox and document unfinished work. Agree any accessibility adjustment before starting.
- Use any language or local database. No required cloud account, paid service or company data. Evaluation runs on Linux with 4 CPU cores, 8 GiB RAM and 10 GiB free disk.
- Supply a Dockerfile and an executable `run` adapter. The runtime must work offline after the image is built. Pin dependencies and document the build command. Your own network access and AI use during development are allowed.
- AI access must be supplied or reimbursed equally by the hiring team. Record model/tool names, approximate spend and active hours; do not include credentials or private account history.

### Read in order
1. `SPECIFICATION.md`: binding semantics, schemas and command contract.
2. `WORKED_EXAMPLES.md`: calculations and expected edge behaviour.
3. `ASSESSMENT.md`: published scoring rubric and hard gates.
4. Inspect `data/`, `queries/` and `expected/`.
5. Run `python3 tools/check_output.py expected/q03.json` to see the public shape checker.

### Submit
Create a **single public GitHub repository** for your submission and send us the URL. No collaborator invite is needed — just make sure the repository is publicly viewable when you submit. All code and all AI usage must live in that repository; we do not accept ZIP files or emailed attachments.

Keep the repository **unchanged once you have submitted the URL**, until evaluation is complete. If you find a defect you must fix, tell us rather than pushing silently.

The repository must contain your source, Dockerfile, `run`, lockfiles, tests and:
- A root `README.md`: how to build and run your system, and where your AI logs live in the repository.
- `DESIGN.md` (maximum 2,000 words): data model, invariants, state transitions, transaction boundary, time semantics, lineage, alternatives and limits.
- `OPERATIONS.md` (maximum 1,200 words): replay/rebuild, schema change, failed-run recovery, deletion, observability, performance measurements and a concrete incident runbook.
- `AI_LOG.md`: five consequential AI interactions, what you checked, at least two suggestions you rejected or corrected (if fewer occurred, say so honestly), tools/models, time and cost.
- **Raw coding-agent logs, committed to the repository.** Upload your coding agent session and chat logs — for example the `.jsonl` session files under `~/.claude/projects/`, or the equivalent export from Codex, Cursor or whichever tool you used — and state their location in your root `README.md`. Because the repository is public, review these before committing: remove credentials, and remove anything from unrelated work or from your own private files that happens to appear in a session. Do not redact your own working process on this exercise. If you used a tool that cannot export logs, say so in `AI_LOG.md` and describe what you used instead.
- `EVIDENCE.md`: exact commands and observed results for correctness, replay, crash recovery, privacy and scale; distinguish measured results from proposed design.
- A short `LIMITATIONS.md` listing anything incomplete. Do not conceal gaps with fabricated logs.

Do not commit generated bulk data, virtual environments, image layers, API keys or any other secret. Use environment variables or a gitignored `.env` file and document how to set them. This matters more than usual in a public repository: a committed key is scraped within minutes, so revoke anything you expose. Fixtures are fully synthetic. The hiring team will run your code on fresh datasets drawn from the same published rules and inspect the implementation. No production-quality UI is required.
