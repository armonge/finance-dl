# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

Install for development:
```
pip install -e .
```

Run the full test suite (pytest + mypy, as CI does):
```
tox
```

Run just the tests, or a single test:
```
pytest .
pytest tests/test_ofx.py::test_sanitize_account_name_disallows_dot
```

Run the type checker on the package:
```
mypy finance_dl
```

Run a single scraper config (`CONFIG_<name>` in a user-supplied config module):
```
python -m finance_dl.cli --config-module example_finance_dl_config --config <name>
```

Debug a scraper interactively (drops into IPython with the scraper module in scope; implies a visible browser):
```
python -m finance_dl.cli --config-module example_finance_dl_config --config <name> -i
```

Show update status / run one or more configs through the parallel updater (writes `logs/<name>.txt` and a `logs/<name>.lastupdate` marker; skips configs updated within 24h unless `-f`):
```
python -m finance_dl.update --config-module example_finance_dl_config --log-dir logs status
python -m finance_dl.update --config-module example_finance_dl_config --log-dir logs update <name> [<name>...] [-f]
python -m finance_dl.update --config-module example_finance_dl_config --log-dir logs update --all
```

## Architecture

This is a library of independent scrapers for personal financial data, plus two CLI drivers and a shared Selenium helper layer.

**Plugin model.** Each data source lives in its own module under `finance_dl/` (e.g. `amazon.py`, `paypal.py`, `ofx.py`). Modules are loaded dynamically by name — they are never imported by `cli.py` or `update.py` directly. The user writes a config module containing `CONFIG_<name>()` functions; each returns a `dict` whose `module` key names the scraper module to import (e.g. `'finance_dl.amazon'`). Adding a new data source means adding a new module — there is no central registry to update.

**Scraper module contract.** A scraper module must expose:
- `run(**spec)` — entry point used by `finance-dl` and the updater. Receives the config dict minus `module`, plus an injected `headless` key.
- `interactive(**spec)` — optional context manager used by `-i`. If present, `cli.py` enters it and starts IPython with `self` (the scraper instance) bound; the docstring convention is that the user then types `self.run()`.

**Two scraper styles.**
- *OFX-protocol modules* (`ofx.py`, plus OFX-based configs like `vanguard`, `discover`) use `ofxclient` over HTTP — no browser.
- *Selenium-based modules* (most others) subclass or instantiate `scrape_lib.Scraper` to drive Chrome via `chromedriver_binary`. They typically take `credentials`, `output_directory`, `profile_dir`, and `headless`.

**`scrape_lib.Scraper`** is the shared base for Selenium scrapers. Key behaviors that matter when modifying scrapers:
- Configures Chrome with a custom download dir, disables PDF viewers so PDFs download instead of rendering, and supports `profile_dir` for a persistent profile (needed to avoid re-doing MFA on every run).
- `headless='--headless=new'` is the default; `cli.py` flips this off for `-i` or `--visible`.
- Honors `CHROMEDRIVER_CHROME_BINARY` env var to point at a non-default Chrome install (see README "Note on Chromedriver Versioning" — Chrome and `chromedriver_binary` versions must match).
- `chromedriver_bin` defaults to the `finance-dl-chromedriver-wrapper` console script (`chromedriver_wrapper.py`), not the system chromedriver.
- Provides helpers used throughout the scrapers: `wait_for_page_load`, `wait_and_locate`, `find_elements_in_any_frame`, `for_each_frame`, `get_downloaded_file`, `extract_table_data`. Prefer these over re-implementing waits/frame traversal.

**CLI fallback for selenium scrapers.** Per the README, if a headless selenium run fails, the runner is expected to retry with a visible browser so the user can complete MFA manually. When touching `cli.py` or scraper retry logic, preserve this behavior.

**Updater (`update.py`).** Runs each config as a subprocess of `python -m finance_dl.cli` so failures stay isolated, captures stdout+stderr to `logs/<name>.txt`, and on success touches `logs/<name>.lastupdate`. Parallelism defaults to 4 (`-p`). Configs updated within the past 24h are skipped unless `-f`.

**Output conventions.** Scrapers are expected to write idempotently into `output_directory`; many use `atomicwrites.atomic_write` to avoid partial files. Filenames typically encode the natural key from the source (order ID, statement date range, account number) so re-runs don't produce duplicates.

## Conventions

- Formatting: yapf with `.style.yapf` (80-col, custom Google-ish style). Not enforced in CI, but match it when editing.
- Type checking: `mypy` runs on `finance_dl/` in CI with `ignore_missing_imports = True`. New code should at least not introduce new mypy errors.
- Versioning is derived from git tags via `setuptools_scm` — don't hand-edit a version string.
- Tests live in `tests/` and are minimal (only `test_ofx.py` currently). End-to-end scraper testing requires real credentials, so most logic is exercised manually via `-i`.
