#!/usr/bin/env python3
"""Tag untagged cross-namespace redirects (XNRs) with the correct rcat
template, per Wikipedia:Template_index/Redirect_pages#To_namespaces.

Candidates come from untagged_redirects_querry.sql, run against the enwiki
replica database. Every candidate is re-verified live via the API before any
edit is made or even printed: in-scope namespace pair, not a WP:CSD#R2
speedy-deletion candidate, not nominated for deletion (RfD/CSD), still a
redirect, still points to the same target, not edited in the last
MIN_AGE_HOURS, still untagged, and not excluded via {{bots}}/{{nobots}}. See
xnrbot/bot.py's evaluate_candidates() for the exact check order.

Examples:
    # Dry run: fetch candidates, verify them, print what would change.
    python3 run_bot.py --dry-run --diff

    # BRFA trial: make up to 25 live edits, logging as it goes.
    python3 run_bot.py --limit 25

    # Full run.
    python3 run_bot.py --limit 500

    # Local testing against real enwiki data, no Toolforge access needed.
    python3 run_bot.py --candidates-file sample_candidates_enwiki.csv --dry-run --diff

    # Local testing against the real replica DB, tunnelled from off Toolforge
    # (see README.md's "Local testing" section for the ssh -L command this
    # expects already running):
    python3 run_bot.py --db-host 127.0.0.1 --db-port 4711 --dry-run --diff

This only ever runs against English Wikipedia -- see README.md for one-time
setup on Toolforge, and its "Local testing" section for testing without any
Toolforge access at all.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import difflib
import logging
import random
import sys
from pathlib import Path

import pymysql
import pywikibot
import toolforge

from xnrbot.bot import (
    EditPlan,
    SkippedCandidate,
    evaluate_candidates,
    fetch_candidates,
    fetch_candidates_from_csv,
)

logger = logging.getLogger("xnrbot")

# --- Constants -- edit directly to change these; only DEFAULT_EDIT_LIMIT is
# also overridable per-run, via --limit. -------------------------------------
# Used only for the descriptive User-Agent sent with replica DB / API traffic
# (WP:UA policy); pywikibot's own login identity comes from user-config.py,
# not from here.
BOT_USERNAME = "Rusabot"
CONTACT_URL = f"https://en.wikipedia.org/wiki/User:{BOT_USERNAME}"

DB_NAME = "enwiki"
# "analytics" is the correct cluster for ad-hoc/reporting-style queries like
# this one; "web" is for latency-sensitive, user-facing tool queries. Edit
# directly if that ever needs to change; not exposed as a flag.
DB_CLUSTER = "analytics"

# toolforge.connect() derives this same name internally; --db-host bypasses
# it (e.g. an SSH tunnel) and needs the real database name directly.
DB_NAME_DIRECT = f"{DB_NAME}_p"

# Redirects edited more recently than this are skipped, to avoid stomping on
# active editing or tagging pages that get deleted shortly after creation.
# Edit the constant directly if this ever needs to change; not exposed as a
# flag.
MIN_AGE_HOURS = 24.0

# Cap on actual edits made per run. Keep this low (e.g. 10-50) for BRFA
# trials; raise only after trial approval.
DEFAULT_EDIT_LIMIT = 50

# Consecutive unexpected errors before the run aborts outright, rather than
# grinding through the rest of the candidate list with (presumably) the same
# problem -- e.g. a broken login or a network outage.
MAX_CONSECUTIVE_ERRORS = 5


def unified_diff(old_text: str, new_text: str, title: str) -> str:
    # A bare "#REDIRECT [[X]]" with no trailing newline is common. If
    # new_text is old_text with something purely appended, pad old_text's
    # missing newline first -- otherwise difflib sees the last line as "X"
    # vs "X\n" and reports it as changed even though it wasn't.
    if not old_text.endswith("\n") and new_text.startswith(old_text):
        old_text += "\n"

    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{title} (before)",
            tofile=f"{title} (after)",
        )
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not save any edits; just report what would change.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print a unified diff for each page, whether edited or dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_EDIT_LIMIT,
        help=f"Maximum number of edits (or would-be edits in --dry-run) to make "
        f"this run. Keep this low for BRFA trials. Default: {DEFAULT_EDIT_LIMIT}.",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Shuffle candidates before applying --limit, instead of taking them in "
        "query order. Useful for a BRFA trial so it isn't all one redirect type.",
    )
    parser.add_argument(
        "--candidates-file",
        default=None,
        metavar="PATH",
        help="Read candidates from this local CSV instead of the replica "
        "database (columns: source_title,target_title,source_ns,target_ns -- "
        "e.g. exported from the query on Quarry, or hand-written for local "
        "testing). Skips the DB connection entirely.",
    )
    parser.add_argument(
        "--db-host",
        default=None,
        help="Connect directly to this host instead of via toolforge.connect() -- "
        "e.g. 127.0.0.1 for an SSH tunnel to the replica DB from off Toolforge "
        "(see README.md's 'Local testing' section). Ignored with --candidates-file.",
    )
    parser.add_argument(
        "--db-port",
        type=int,
        default=4711,
        help="Used only together with --db-host. Default: %(default)s.",
    )
    parser.add_argument(
        "--replica-cnf",
        default=str(Path(__file__).resolve().parent / "replica.my.cnf"),
        metavar="PATH",
        help="MySQL option file with replica credentials, used only together "
        "with --db-host. Default: %(default)s.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-level logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # toolforge.set_user_agent() only affects the bare `requests` calls the
    # `toolforge` package itself makes (e.g. its sitematrix helper); it does
    # NOT change pywikibot's own outgoing User-Agent, which has its own
    # config knob (WP:UA policy compliance needs both set).
    toolforge.set_user_agent(BOT_USERNAME.lower(), url=CONTACT_URL)
    pywikibot.config.user_agent_description = f"{BOT_USERNAME}; {CONTACT_URL}"

    site = pywikibot.Site("en", "wikipedia")
    if not args.dry_run:
        # Not needed for --dry-run: every write path is skipped
        site.login()

    if args.candidates_file:
        candidates = fetch_candidates_from_csv(args.candidates_file)
        logger.info("Loaded %d candidate redirect(s) from %s.", len(candidates), args.candidates_file)
    else:
        if args.db_host:
            connection = pymysql.connect(
                host=args.db_host,
                port=args.db_port,
                database=DB_NAME_DIRECT,
                read_default_file=args.replica_cnf,
                charset="utf8mb4",
            )
        else:
            connection = toolforge.connect(DB_NAME, cluster=DB_CLUSTER, charset="utf8mb4")
        try:
            candidates = fetch_candidates(connection)
        finally:
            connection.close()
        logger.info("Fetched %d candidate redirect(s) from the replica database.", len(candidates))

    if args.random:
        random.shuffle(candidates)

    min_age = datetime.timedelta(hours=MIN_AGE_HOURS)
    edits_made = 0
    skip_counts: collections.Counter = collections.Counter()
    consecutive_errors = 0

    def note_error() -> bool:
        """Bump the consecutive-error count; return True if the run should
        abort. Shared between the evaluation-error and save-error cases
        below so MAX_CONSECUTIVE_ERRORS is enforced identically either way.
        """
        nonlocal consecutive_errors
        consecutive_errors += 1
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            logger.error("%d consecutive errors; aborting the rest of this run.", consecutive_errors)
            return True
        return False

    # A pywikibot.exceptions.Error from evaluating one candidate comes back
    # as that candidate's own result (see evaluate_candidates()'s docstring)
    # rather than propagating, so it's handled via isinstance() here instead
    # of a try/except around the loop body.
    for candidate, result in evaluate_candidates(site, candidates, min_age):
        if edits_made >= args.limit:
            logger.info("Reached --limit of %d edit(s); stopping.", args.limit)
            break

        if isinstance(result, pywikibot.exceptions.Error):
            logger.error("Error evaluating %s; skipping it.", candidate.source_title, exc_info=result)
            if note_error():
                return 1
            continue

        if isinstance(result, SkippedCandidate):
            skip_counts[result.reason.value] += 1
            logger.info(
                "SKIP %s: %s%s",
                candidate.source_title,
                result.reason.value,
                f" ({result.detail})" if result.detail else "",
            )
            consecutive_errors = 0
            continue

        plan: EditPlan = result
        if args.diff:
            print(unified_diff(plan.old_text, plan.new_text, plan.page.title()))

        try:
            if args.dry_run:
                logger.info(
                    "DRY RUN would tag [[%s]] with {{%s}} -- summary: %r",
                    plan.page.title(),
                    plan.template_name,
                    plan.summary,
                )
            else:
                plan.page.text = plan.new_text
                # Explicit, not just the default: leaving it unset falls
                # back to the operator's global cosmetic_changes setting,
                # which could silently fold unrelated changes into this edit.
                plan.page.save(summary=plan.summary, minor=False, bot=True, apply_cosmetic_changes=False)
                logger.info("EDITED [[%s]] with {{%s}}", plan.page.title(), plan.template_name)

            edits_made += 1
            consecutive_errors = 0

        except pywikibot.exceptions.Error:
            logger.exception("Error saving %s; skipping it.", candidate.source_title)
            if note_error():
                return 1

    logger.info(
        "Done. %d edit(s) %s. Skipped: %s",
        edits_made,
        "would be made" if args.dry_run else "made",
        dict(skip_counts) or "none",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
