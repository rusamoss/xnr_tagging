"""Fetch candidate redirects, live-verify each one, and build the edit --
or explain why it's skipped. No network writes here; that's run_bot.py's
job.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime
import enum
import itertools
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path

import pywikibot

from .policy import (
    CSD_TRACKING_CATEGORY,
    RFD_TRACKING_CATEGORY,
    XNR_TAGGED_CATEGORIES,
    add_rcat,
    is_r2_eligible,
    scope_violation,
    template_for_target_namespace,
)

_QUERY_PATH = Path(__file__).resolve().parent.parent / "untagged_redirects_querry.sql"


@dataclasses.dataclass(frozen=True)
class Candidate:
    source_title: str # no namespace prefix
    target_title: str
    source_namespace: int
    target_namespace: int


def _decode_title(value: str | bytes) -> str:
    """MediaWiki stores page_title/rd_title as VARBINARY, not VARCHAR, so
    pymysql returns them as raw bytes rather than auto-decoding the way it
    does for ordinary text columns -- UTF-8 by MediaWiki's own convention,
    not something the DB driver can infer from column metadata alone.
    """
    return value.decode("utf-8") if isinstance(value, bytes) else value


def fetch_candidates(connection) -> list[Candidate]:
    """Run the discovery query and return candidate redirects. connection is
    a DB-API connection, e.g. from toolforge.connect("enwiki", ...).
    """
    with connection.cursor() as cursor:
        cursor.execute(_QUERY_PATH.read_text(encoding="utf-8"))
        rows = cursor.fetchall()

    # Row order (source_title, target_title, source_ns, target_ns) matches
    # Candidate's field order exactly.
    return [
        Candidate(_decode_title(row[0]), _decode_title(row[1]), row[2], row[3]) for row in rows
    ]


def fetch_candidates_from_csv(path: str | Path) -> list[Candidate]:
    """Read candidates from a local CSV instead of the replica database.
    Expects a header row: source_title,target_title,source_ns,target_ns.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            Candidate(
                source_title=row["source_title"],
                target_title=row["target_title"],
                source_namespace=int(row["source_ns"]),
                target_namespace=int(row["target_ns"]),
            )
            for row in reader
        ]


logger = logging.getLogger("xnrbot")


class SkipReason(enum.Enum):
    OUT_OF_SCOPE = "namespace pair is out of scope"
    R2_ELIGIBLE = "mainspace redirect to a non-exempt namespace is WP:CSD#R2-eligible, not a tagging candidate"
    NO_TEMPLATE = "no rcat template is defined for this target namespace"
    INVALID_NAMESPACE = "namespace ID not recognized by the live site"
    NOT_FOUND = "page no longer exists"
    PENDING_DELETION = "page is nominated for deletion (RfD or CSD)"
    NOT_REDIRECT = "page is no longer a redirect"
    TARGET_CHANGED = "redirect target has changed since the query ran"
    RECENTLY_EDITED = "edited too recently"
    ALREADY_TAGGED = "already has the XNR category (tagged since the query ran)"
    BOT_EXCLUDED = "page opts out via {{bots}}/{{nobots}}"
    NO_CHANGE = "template already present in wikitext (no-op)"


@dataclasses.dataclass
class SkippedCandidate:
    reason: SkipReason
    detail: str = ""


@dataclasses.dataclass
class EditPlan:
    page: pywikibot.Page
    template_name: str
    old_text: str
    new_text: str
    summary: str


def _resolve_title(site: pywikibot.Site, raw_title: str, namespace_id: int) -> str:
    """Build a "Namespace:Title" string using the site's live namespace
    names, not pywikibot.Page's `ns=` param -- `ns=` is silently overridden
    when the title itself looks prefixed (`Page(site, "Portal:Foo", ns=118)`
    resolves to Portal:Foo, not Draft:Portal:Foo).
    """
    ns_name = site.namespace(namespace_id)
    return f"{ns_name}:{raw_title}" if ns_name else raw_title


def _resolve_or_skip(site: pywikibot.Site, raw_title: str, namespace_id: int) -> str | SkippedCandidate:
    """_resolve_title(), turning the KeyError it can raise (namespace ID the
    live site doesn't recognize -- only reachable via a malformed
    --candidates-file row) into a SkippedCandidate instead of propagating.
    """
    try:
        return _resolve_title(site, raw_title, namespace_id)
    except KeyError:
        return SkippedCandidate(SkipReason.INVALID_NAMESPACE, detail=str(namespace_id))


def _prepare_candidate(
    site: pywikibot.Site, candidate: Candidate
) -> SkippedCandidate | tuple[pywikibot.Page, str]:
    """The network-free part of evaluating a candidate: cheap scope/template
    checks plus constructing (not fetching) the source page. Split out so
    evaluate_candidates() can preload only the pages that survive this.
    """
    violation = scope_violation(candidate.source_namespace, candidate.target_namespace)
    if violation is not None:
        return SkippedCandidate(SkipReason.OUT_OF_SCOPE, detail=violation)

    if is_r2_eligible(candidate.source_namespace, candidate.target_namespace):
        return SkippedCandidate(SkipReason.R2_ELIGIBLE)

    mapping = template_for_target_namespace(candidate.target_namespace)
    if mapping is None:
        return SkippedCandidate(SkipReason.NO_TEMPLATE)
    template_name, _ = mapping

    source_title = _resolve_or_skip(site, candidate.source_title, candidate.source_namespace)
    if isinstance(source_title, SkippedCandidate):
        return source_title

    return pywikibot.Page(site, source_title), template_name


def _evaluate_prepared_candidate(
    site: pywikibot.Site,
    candidate: Candidate,
    page: pywikibot.Page,
    template_name: str,
    min_age: datetime.timedelta,
) -> EditPlan | SkippedCandidate:
    """The rest of evaluating a candidate: every check that needs a network
    round trip, continuing from an already-constructed *page* (may already
    be preloaded; see evaluate_candidates()).
    """
    if not page.exists():
        return SkippedCandidate(SkipReason.NOT_FOUND)

    # Checked via live categories, not wikitext, and before isRedirectPage():
    # an RfD nomination substitutes onto the page, wrapping "#REDIRECT [[X]]"
    # in a template call that also breaks isRedirectPage() -- so a wikitext
    # scan for {{Rfd}} would find nothing. CSD tags are conventionally placed
    # above the redirect line (not guaranteed), so this check is
    # defense-in-depth for when one isn't.
    live_categories = {cat.title(with_ns=False) for cat in page.categories()}
    if RFD_TRACKING_CATEGORY in live_categories:
        return SkippedCandidate(SkipReason.PENDING_DELETION, detail="RfD nomination")
    if CSD_TRACKING_CATEGORY in live_categories:
        return SkippedCandidate(SkipReason.PENDING_DELETION, detail="CSD nomination")

    if not page.isRedirectPage():
        return SkippedCandidate(SkipReason.NOT_REDIRECT)

    try:
        current_target = page.getRedirectTarget()
    except pywikibot.exceptions.Error as exc:
        return SkippedCandidate(SkipReason.NOT_REDIRECT, str(exc))

    # Compare ignoring section fragments: the SQL only tracks rd_namespace/
    # rd_title, not rd_fragment, so "Page#Section" still matches target_title="Page".
    target_title = _resolve_or_skip(site, candidate.target_title, candidate.target_namespace)
    if isinstance(target_title, SkippedCandidate):
        return target_title
    expected_target = pywikibot.Page(site, target_title)
    if current_target.title(with_section=False) != expected_target.title(with_section=False):
        return SkippedCandidate(
            SkipReason.TARGET_CHANGED, detail=f"now points to {current_target.title()}"
        )

    last_edit = page.latest_revision.timestamp
    # Revision timestamps parsed from the API are naive (both UTC).
    now = pywikibot.Timestamp.nowutc(with_tz=False)
    if now - last_edit < min_age:
        return SkippedCandidate(SkipReason.RECENTLY_EDITED, detail=f"last edited {last_edit.isoformat()}")

    # Intersect against the *full* XNR whitelist, not just this candidate's
    # own category: the SQL excludes a redirect carrying *any* of those
    # categories (e.g. Redirects_from_old_AfC_drafts has no target-namespace
    # mapping of its own), so checking only the single matching category
    # here would silently re-tag pages the query already considers tagged.
    if live_categories & XNR_TAGGED_CATEGORIES:
        return SkippedCandidate(SkipReason.ALREADY_TAGGED)

    if not page.botMayEdit():
        return SkippedCandidate(SkipReason.BOT_EXCLUDED)

    old_text = page.text
    new_text = add_rcat(old_text, template_name)
    if new_text == old_text:
        return SkippedCandidate(SkipReason.NO_CHANGE)

    return EditPlan(
        page=page,
        template_name=template_name,
        old_text=old_text,
        new_text=new_text,
        summary=f"Tagging [[WP:XNR|cross-namespace redirect]] with {{{{{template_name}}}}} ([[Wikipedia:Bots/Requests for approval/Rusabot|BRFA]])",
    )


def _chunked(iterable: Iterable[Candidate], size: int) -> Iterator[list[Candidate]]:
    it = iter(iterable)
    while chunk := list(itertools.islice(it, size)):
        yield chunk


def evaluate_candidates(
    site: pywikibot.Site,
    candidates: Iterable[Candidate],
    min_age: datetime.timedelta,
    *,
    groupsize: int = 50,
) -> Iterator[tuple[Candidate, EditPlan | SkippedCandidate | pywikibot.exceptions.Error]]:
    """Live-verify each *candidate*: build an EditPlan for it, or a
    SkippedCandidate with the reason (see SkipReason). Yields (candidate,
    result) pairs in original order, *groupsize* at a time.

    Every source page surviving _prepare_candidate()'s cheap checks is
    preloaded in one site.preloadpages(..., categories=True) call per
    group, collapsing 3 round trips (exists/categories/revision) into 1;
    getRedirectTarget() still isn't covered, so that stays per-candidate.
    Chunked, not preloaded all at once, so an early-stopping caller (e.g.
    --limit) doesn't pay for a group it never looks at.

    A pywikibot.exceptions.Error from one candidate is caught and yielded
    as its result rather than propagating and losing every candidate
    queued behind it -- callers should check `isinstance(result,
    pywikibot.exceptions.Error)`.
    """
    for chunk in _chunked(candidates, groupsize):
        prepared: list[SkippedCandidate | pywikibot.exceptions.Error | tuple[pywikibot.Page, str]] = []
        to_preload: list[pywikibot.Page] = []
        for candidate in chunk:
            try:
                result = _prepare_candidate(site, candidate)
            except pywikibot.exceptions.Error as exc:
                prepared.append(exc)
                continue
            prepared.append(result)
            if not isinstance(result, SkippedCandidate):
                to_preload.append(result[0])

        if to_preload:
            try:
                for _ in site.preloadpages(to_preload, groupsize=groupsize, categories=True):
                    pass
            except pywikibot.exceptions.Error:
                # Preloading is an optimization, not a dependency: if the
                # batched request fails, fall through and let each
                # candidate make its own (unbatched) requests
                logger.warning(
                    "Batch preload failed for a group of %d page(s); falling back to per-page requests.",
                    len(to_preload),
                )

        for candidate, result in zip(chunk, prepared):
            if isinstance(result, (SkippedCandidate, pywikibot.exceptions.Error)):
                yield candidate, result
                continue
            page, template_name = result
            try:
                yield candidate, _evaluate_prepared_candidate(
                    site, candidate, page, template_name, min_age
                )
            except pywikibot.exceptions.Error as exc:
                yield candidate, exc

