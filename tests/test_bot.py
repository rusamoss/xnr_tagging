import datetime
from pathlib import Path

import pywikibot
import pytest

from xnrbot.bot import (
    Candidate,
    EditPlan,
    SkipReason,
    SkippedCandidate,
    _resolve_title,
    evaluate_candidates,
    fetch_candidates,
    fetch_candidates_from_csv,
)
from xnrbot.policy import (
    CATEGORY_NAMESPACE,
    DRAFT_NAMESPACE,
    MAIN_NAMESPACE,
    TEMPLATE_NAMESPACE,
    USER_NAMESPACE,
    WIKIPEDIA_NAMESPACE,
)

NOW = pywikibot.Timestamp.nowutc().replace(tzinfo=None)
MIN_AGE = datetime.timedelta(hours=24)


def _evaluate(site, candidate, min_age):
    """Evaluate a single candidate via evaluate_candidates()"""
    return next(iter(evaluate_candidates(site, [candidate], min_age)))[1]


class FakeRevision:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeCategory:
    def __init__(self, name):
        self._name = name

    def title(self, with_ns=True):  # noqa: U100 -- matches pywikibot.Page.title's signature
        return self._name


class FakeSite:
    """Stand-in for pywikibot.Site: site.namespace() (used by _resolve_title)
    and site.preloadpages() (used by evaluate_candidates()). FakePage has no
    lazy-load state, so preloadpages() here just records calls for
    batching-behavior assertions and passes pages through unchanged.
    """

    _NAMES = {
        MAIN_NAMESPACE: "",
        USER_NAMESPACE: "User",
        WIKIPEDIA_NAMESPACE: "Wikipedia",
        5: "Wikipedia talk",
        TEMPLATE_NAMESPACE: "Template",
        CATEGORY_NAMESPACE: "Category",
        DRAFT_NAMESPACE: "Draft",
    }

    def __init__(self):
        self.preload_calls: list[list[str]] = []
        self.preload_error = None

    def namespace(self, namespace_id):
        return self._NAMES[namespace_id]

    def preloadpages(self, pages, groupsize=50, categories=False):  # noqa: U100
        self.preload_calls.append([page.title() for page in pages])
        if self.preload_error is not None:
            raise self.preload_error
        yield from pages


class FakePage:
    """Minimal stand-in for pywikibot.Page covering only what evaluate_candidates touches."""

    def __init__(
        self,
        title,
        *,
        exists=True,
        is_redirect=True,
        redirect_target_title=None,
        last_edit=NOW - datetime.timedelta(days=10),
        category_names=(),
        bot_may_edit=True,
        has_permission=True,
        protection_level="sysop",
        text="#REDIRECT [[Target]]\n",
        redirect_error=None,
    ):
        self._title = title.replace("_", " ")
        self._exists = exists
        self._is_redirect = is_redirect
        self._redirect_target_title = redirect_target_title
        self._redirect_error = redirect_error
        self.latest_revision = FakeRevision(last_edit)
        self._categories = [FakeCategory(name) for name in category_names]
        self._bot_may_edit = bot_may_edit
        self._has_permission = has_permission
        self._protection_level = protection_level
        self.text = text

    def title(self, with_section=True):
        return self._title if with_section else self._title.split("#", 1)[0]

    def exists(self):
        return self._exists

    def isRedirectPage(self):
        return self._is_redirect

    def getRedirectTarget(self):
        if self._redirect_error is not None:
            raise self._redirect_error
        return FakePage(self._redirect_target_title)

    def categories(self):
        return self._categories

    def botMayEdit(self):
        return self._bot_may_edit

    def has_permission(self, action="edit"):  # noqa: U100 -- action unused, matches real signature
        return self._has_permission

    def protection(self):
        return {"edit": (self._protection_level, "infinity")} if not self._has_permission else {}


@pytest.fixture
def pages():
    """title (normalized) -> FakePage, consulted by the patched pywikibot.Page()."""
    return {}


@pytest.fixture(autouse=True)
def patch_pywikibot_page(monkeypatch, pages):
    def fake_page_constructor(site, title):  # noqa: U100 -- site unused, matches real signature
        key = title.replace("_", " ")
        if key not in pages:
            pages[key] = FakePage(key, exists=False)
        return pages[key]

    monkeypatch.setattr("xnrbot.bot.pywikibot.Page", fake_page_constructor)


SITE = FakeSite()


def test_resolve_title_prepends_the_live_namespace_name():
    assert _resolve_title(SITE, "Foo", CATEGORY_NAMESPACE) == "Category:Foo"
    assert _resolve_title(SITE, "Foo", MAIN_NAMESPACE) == "Foo"  # mainspace has no prefix


def test_resolve_title_handles_a_title_that_itself_looks_like_a_namespace_prefix():
    # Real edge case: a page titled "Draft:Portal:Foo" has raw page_title
    # "Portal:Foo" -- see _resolve_title()'s docstring for why concatenation
    # (not ns=) is required here. FakeSite has no "Portal" entry, confirming
    # _resolve_title() never even looks it up.
    assert _resolve_title(SITE, "Portal:Foo", DRAFT_NAMESPACE) == "Draft:Portal:Foo"


# Raw titles (no namespace prefix) -- _resolve_title() turns these into
# "Category:Foo" etc. via SITE.namespace() before any Page gets constructed.
CANDIDATE = Candidate(
    source_title="Foo", target_title="Foo", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE
)


def test_evaluate_candidate_happy_path_returns_an_edit_plan(pages):
    pages["Foo"] = FakePage(
        "Foo",
        redirect_target_title="Category:Foo",
        text="#REDIRECT [[Category:Foo]]\n",
    )

    result = _evaluate(SITE, CANDIDATE, MIN_AGE)

    assert isinstance(result, EditPlan)
    assert result.template_name == "R to category namespace"
    assert "{{R to category namespace}}" in result.new_text
    assert result.summary == (
        "Tagging [[WP:XNR|cross-namespace redirect]] with {{R to category namespace}} "
        "([[Wikipedia:Bots/Requests for approval/Rusabot|BRFA]])"
    )


@pytest.mark.parametrize(
    "candidate, expected_reason",
    [
        # Defense-in-depth for --candidates-file: a hand-edited CSV could
        # contain a pair the real SQL query would never produce. All four
        # rejected before any pywikibot.Page is constructed, confirmed below
        # via `pages == {}`.
        (Candidate("Foo", "Foo", DRAFT_NAMESPACE, CATEGORY_NAMESPACE), SkipReason.OUT_OF_SCOPE),
        # File (6): has no XNR rcat template defined at all.
        (Candidate("Foo", "Foo.png", TEMPLATE_NAMESPACE, 6), SkipReason.NO_TEMPLATE),
        # Real example: mainspace -> Draft: (see CLAUDE.md's R2 notes).
        (Candidate("APatch", "APatch", MAIN_NAMESPACE, DRAFT_NAMESPACE), SkipReason.R2_ELIGIBLE),
        # FakeSite._NAMES doesn't know 999 -- mirrors a malformed
        # --candidates-file row that site.namespace() can't recognize.
        (Candidate("Foo", "Foo", 999, CATEGORY_NAMESPACE), SkipReason.INVALID_NAMESPACE),
    ],
)
def test_evaluate_candidate_rejects_bad_namespace_pairs_without_touching_any_page(
    pages, candidate, expected_reason
):
    result = _evaluate(SITE, candidate, MIN_AGE)

    assert isinstance(result, SkippedCandidate)
    assert result.reason is expected_reason
    assert pages == {}


# Every other reason a candidate gets skipped rather than tagged -- each a
# FakePage state plus the SkipReason it must produce (and sometimes a
# substring of the detail message).
@pytest.mark.parametrize(
    "page_kwargs, expected_reason, detail_contains",
    [
        (dict(exists=False), SkipReason.NOT_FOUND, None),
        (dict(is_redirect=False), SkipReason.NOT_REDIRECT, None),
        (
            # Distinct from is_redirect=False: here isRedirectPage() is True
            # but getRedirectTarget() itself raises -- a separate guard
            # clause, not just another way to hit the same line.
            dict(redirect_error=pywikibot.exceptions.CircularRedirectError(FakePage("Foo"))),
            SkipReason.NOT_REDIRECT,
            None,
        ),
        (dict(redirect_target_title="Category:Something else"), SkipReason.TARGET_CHANGED, "Something else"),
        (
            dict(redirect_target_title="Category:Foo", last_edit=NOW - datetime.timedelta(hours=1)),
            SkipReason.RECENTLY_EDITED,
            None,
        ),
        (
            dict(redirect_target_title="Category:Foo", category_names=("Redirects to category space",)),
            SkipReason.ALREADY_TAGGED,
            None,
        ),
        (dict(redirect_target_title="Category:Foo", bot_may_edit=False), SkipReason.BOT_EXCLUDED, None),
        (
            # Real trigger: pywikibot.exceptions.LockedPageError from
            # page.save() on a fully-protected page (e.g. Main Page/sandbox)
            # -- caught proactively here instead of surfacing as a save-time
            # crash.
            dict(redirect_target_title="Category:Foo", has_permission=False, protection_level="sysop"),
            SkipReason.PROTECTED,
            "sysop",
        ),
        (
            # Defensive case: category membership hasn't caught it (e.g.
            # cache lag), but the raw wikitext already has the template.
            dict(
                redirect_target_title="Category:Foo",
                text="#REDIRECT [[Category:Foo]]\n\n{{R to category namespace}}\n",
            ),
            SkipReason.NO_CHANGE,
            None,
        ),
        (
            # RFD_TRACKING_CATEGORY and CSD_TRACKING_CATEGORY are two
            # separate `if` checks in the source -- both are covered here,
            # not just one standing in for both.
            dict(redirect_target_title="Category:Foo", category_names=("All redirects for discussion",)),
            SkipReason.PENDING_DELETION,
            "RfD",
        ),
        (
            dict(redirect_target_title="Category:Foo", category_names=("Candidates for speedy deletion",)),
            SkipReason.PENDING_DELETION,
            "CSD",
        ),
    ],
)
def test_evaluate_candidate_skip_reasons(pages, page_kwargs, expected_reason, detail_contains):
    pages["Foo"] = FakePage("Foo", **page_kwargs)

    result = _evaluate(SITE, CANDIDATE, MIN_AGE)

    assert isinstance(result, SkippedCandidate)
    assert result.reason is expected_reason
    if detail_contains:
        assert detail_contains in result.detail


def test_evaluate_candidate_already_tagged_checks_the_full_xnr_category_whitelist(pages):
    # AfC exception: source-based, not tied to target namespace, so it'd be
    # missed by a check keyed only on the single matching category (see
    # _evaluate_prepared_candidate's ALREADY_TAGGED comment). Real pattern:
    # Wikipedia talk (old AfC submission talk subpage) -> Main (once accepted).
    candidate = Candidate("Foo", "Foo", source_namespace=5, target_namespace=MAIN_NAMESPACE)
    pages["Wikipedia talk:Foo"] = FakePage(
        "Wikipedia talk:Foo",
        redirect_target_title="Foo",
        category_names=("Redirects from old AfC drafts",),
    )

    result = _evaluate(SITE, candidate, MIN_AGE)

    assert isinstance(result, SkippedCandidate)
    assert result.reason is SkipReason.ALREADY_TAGGED


def test_evaluate_candidate_invalid_target_namespace(pages):
    # Separate _resolve_title() call site from the source-namespace case
    # above -- reached only after the source page is confirmed live. Source
    # is Wikipedia (4) so this doesn't also trip R2; target 999 is odd so
    # template_for_target_namespace() succeeds, and the failure only
    # surfaces when resolving the *target* title.
    candidate = Candidate("Foo", "Foo", source_namespace=WIKIPEDIA_NAMESPACE, target_namespace=999)
    pages["Wikipedia:Foo"] = FakePage("Wikipedia:Foo", redirect_target_title="Bar:Foo")

    result = _evaluate(SITE, candidate, MIN_AGE)

    assert isinstance(result, SkippedCandidate)
    assert result.reason is SkipReason.INVALID_NAMESPACE


def test_evaluate_candidate_tolerates_a_target_section_fragment(pages):
    # The SQL only tracks rd_namespace/rd_title, not rd_fragment, so a
    # redirect landing on "Page#Section" must still match target_title="Page".
    pages["Foo"] = FakePage("Foo", redirect_target_title="Category:Foo#Some section")

    result = _evaluate(SITE, CANDIDATE, MIN_AGE)

    assert isinstance(result, EditPlan)


# --- evaluate_candidates (multi-candidate batching behavior) -----------------
# The tests above cover per-candidate outcomes via the _evaluate() helper
# (a single-item list through evaluate_candidates()); these cover what only
# shows up with more than one candidate: pages get grouped by groupsize, a
# failed batch preload falls back to per-page checks, and one candidate's
# error doesn't take out its group.


def test_evaluate_candidates_handles_a_mixed_batch(pages):
    # OUT_OF_SCOPE is rejected before any page is touched (never in a
    # preload call); the other two are.
    candidates = [
        Candidate("Foo", "Foo", source_namespace=DRAFT_NAMESPACE, target_namespace=CATEGORY_NAMESPACE),
        Candidate("Bar", "Bar", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE),
        Candidate("Baz", "Baz", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE),
    ]
    pages["Bar"] = FakePage(
        "Bar", redirect_target_title="Category:Bar", text="#REDIRECT [[Category:Bar]]\n"
    )
    pages["Baz"] = FakePage(
        "Baz", redirect_target_title="Category:Baz", category_names=("Redirects to category space",)
    )
    site = FakeSite()

    results = list(evaluate_candidates(site, candidates, MIN_AGE))

    assert [c.source_title for c, _ in results] == ["Foo", "Bar", "Baz"]
    assert results[0][1] == SkippedCandidate(
        SkipReason.OUT_OF_SCOPE, detail="source namespace is excluded (User, User talk, File, MediaWiki, or Draft)"
    )
    assert isinstance(results[1][1], EditPlan)
    assert results[2][1] == SkippedCandidate(SkipReason.ALREADY_TAGGED)
    assert site.preload_calls == [["Bar", "Baz"]]


def test_evaluate_candidates_groups_preload_calls_by_groupsize(pages):
    candidates = [
        Candidate(f"Page{i}", f"Page{i}", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE)
        for i in range(5)
    ]
    for i in range(5):
        pages[f"Page{i}"] = FakePage(f"Page{i}", redirect_target_title=f"Category:Page{i}")
    site = FakeSite()

    results = list(evaluate_candidates(site, candidates, MIN_AGE, groupsize=2))

    assert len(results) == 5
    assert all(isinstance(result, EditPlan) for _, result in results)
    assert site.preload_calls == [["Page0", "Page1"], ["Page2", "Page3"], ["Page4"]]


def test_evaluate_candidates_falls_back_to_individual_checks_if_batch_preload_fails(pages):
    # Preloading is an optimization, not a correctness dependency: if the
    # one batched request backing a whole group fails, every candidate in
    # that group must still be evaluated correctly via its own (unbatched)
    # checks, not silently dropped or misreported.
    pages["Foo"] = FakePage("Foo", redirect_target_title="Category:Foo")
    site = FakeSite()
    site.preload_error = pywikibot.exceptions.ServerError("simulated outage")

    results = list(evaluate_candidates(site, [CANDIDATE], MIN_AGE))

    assert len(results) == 1
    assert isinstance(results[0][1], EditPlan)


def test_evaluate_candidates_isolates_a_live_check_error_to_its_own_candidate(pages):
    class ExplodingPage(FakePage):
        def exists(self):
            raise pywikibot.exceptions.ServerError("simulated outage")

    pages["Bad"] = ExplodingPage("Bad")
    pages["Good"] = FakePage("Good", redirect_target_title="Category:Good")
    candidates = [
        Candidate("Bad", "Bad", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE),
        Candidate("Good", "Good", source_namespace=MAIN_NAMESPACE, target_namespace=CATEGORY_NAMESPACE),
    ]
    site = FakeSite()

    results = list(evaluate_candidates(site, candidates, MIN_AGE))

    assert isinstance(results[0][1], pywikibot.exceptions.Error)
    assert isinstance(results[1][1], EditPlan)


# --- fetch_candidates / fetch_candidates_from_csv -----------------------------


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql):
        self.executed_sql = sql

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor


def test_fetch_candidates_runs_query_and_wraps_rows():
    # Rows are raw titles, no namespace prefix, matching the SQL's SELECT.
    # page_title/rd_title are real bytes here, not str: MediaWiki stores
    # them as VARBINARY, so pymysql returns raw bytes for these two columns
    # specifically.
    rows = [
        (b"Foo", b"Foo", 1, 5),  # Talk -> Wikipedia talk
        (b"Bar", b"Bar", MAIN_NAMESPACE, CATEGORY_NAMESPACE),
    ]
    connection = FakeConnection(rows)
    candidates = fetch_candidates(connection)

    assert candidates == [
        Candidate("Foo", "Foo", 1, 5),
        Candidate("Bar", "Bar", MAIN_NAMESPACE, CATEGORY_NAMESPACE),
    ]
    assert "SELECT" in connection._cursor.executed_sql


SAMPLE_CSV_PATH = Path(__file__).parent.parent / "sample_candidates_enwiki.csv"


def test_fetch_candidates_from_csv():
    candidates = fetch_candidates_from_csv(SAMPLE_CSV_PATH)

    assert len(candidates) == 24
    assert candidates[0] == Candidate(
        "American Open Circulation & Vascular Journal",
        "Research and Knowledge Publication academic journals",
        MAIN_NAMESPACE,
        CATEGORY_NAMESPACE,
    )
    # Embedded, non-leading "Portal:" in the raw title -- must not be
    # mistaken for a namespace prefix and stripped by the CSV round trip.
    assert any(c.source_title == "WikiProject_Germany/Portal:Thuringia/March" for c in candidates)
