# XNR tagging bot

This bot tags cross-namespace redirects on the English Wikipedia with the [appropriate redirect category templates](https://en.wikipedia.org/wiki/Wikipedia:Template_index/Redirect_pages#To_namespaces).

Full logic:
1. Find candidate redirects using [this query](untagged_redirects_querry.sql) ([quarry link](https://quarry.wmcloud.org/query/107774)). Redirects must be
  * Cross-namespace
  * Not already tagged with a XNR redirect category
  * Not in Draft: or User: or User talk:
  * Not MOS: -> Wikipedia: (not really cross-namespace since MOS: was a pseudo-namespace in mainspace for so long)
  * Not R2 eligible (e.g., not from mainspace to any other namespace except the Category:, Template:, Wikipedia:, Help:, and Portal: namespaces)
  * Not from any talk namespace to any other talk namespace
  * Targetting a namespace that actually has a corresponding rcat (so, not e.g. to Module:)
  * Not an interwiki redirect
2. For each candidate redirect found by that query, check against the live page if
  * The redirect is still eligible to be tagged
  * The redirect target has not changed from what the query expects
  * Not edited in the last 24 hours
  * Not tagged for deletion template (CSD/RfD)
  * Not tagged with nobots
3. If the redirect is eligible to be tagged, add the correct XNR template, in a {{Redirect category shell}} wrapper if one is not already present. If not, log the reason we're skipping it.
4. If not in `--dry-run` mode, save the edit. If in `--diff` mode, print a diff of the change.

## Run

```bash
python3 run_bot.py --dry-run --diff --limit 25
```

Run `python3 run_bot.py --help` for the full flag list.

## Testing
### Local

This skips the Quarry query step and instead pulls potential XNRs from a CSV, and does a read-only check against the live enwiki page.

```bash
python3 run_bot.py --candidates-file sample_candidates_enwiki.csv --dry-run --diff
```

To run against the [real Quarry query](https://quarry.wmcloud.org/query/107774) output, export it as a CSV.

### Against toolforge database
To use the real toolforge database from your local machine, open a tunnel:

```bash
ssh -N <you>@login.toolforge.org \
    -L 4711:enwiki.analytics.db.svc.wikimedia.cloud:3306
```

then, leaving that running, in another terminal point `run_bot.py` at the tunnel with `--db-host`/`--db-port`:

```bash
python3 run_bot.py --db-host 127.0.0.1 --db-port 4711 --dry-run --diff --limit 25
```

This needs your replica credentials in a `replica.my.cnf` file. `--replica-cnf` defaults to `replica.my.cnf` next to `run_bot.py`; pass it explicitly to use a different path.

### Unit tests

```bash
pip install -r requirements-dev.txt
pytest
```