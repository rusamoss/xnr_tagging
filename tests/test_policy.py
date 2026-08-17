from pathlib import Path

import pytest

from xnrbot.policy import (
    AFC_DRAFT_CATEGORY,
    CATEGORY_NAMESPACE,
    DRAFT_NAMESPACE,
    HELP_NAMESPACE,
    MAIN_NAMESPACE,
    MOS_NAMESPACE,
    PORTAL_NAMESPACE,
    SHELL_ALIASES,
    SHELL_CANONICAL_NAME,
    TALK_TEMPLATE,
    TARGET_NAMESPACE_TEMPLATES,
    TEMPLATE_NAMESPACE,
    USER_NAMESPACE,
    USER_TALK_NAMESPACE,
    WIKIPEDIA_NAMESPACE,
    XNR_TAGGED_CATEGORIES,
    add_rcat,
    is_r2_eligible,
    scope_violation,
    template_for_target_namespace,
)

# --- template_for_target_namespace -------------------------------------------


@pytest.mark.parametrize(
    "namespace_id, expected",
    [
        (CATEGORY_NAMESPACE, ("R to category namespace", "Redirects to category space")),
        (MOS_NAMESPACE, ("R to MOS namespace", "Redirects to MOS namespace")),  # newest, easy to typo
    ],
)
def test_known_target_namespaces_return_their_dedicated_template(namespace_id, expected):
    assert template_for_target_namespace(namespace_id) == expected


def test_odd_namespace_gets_the_generic_talk_template():
    assert template_for_target_namespace(5) == TALK_TEMPLATE  # Wikipedia talk


def test_user_talk_uses_the_generic_talk_template_not_the_user_one():
    # User has its own dedicated template, User talk doesn't
    assert (
        template_for_target_namespace(USER_TALK_NAMESPACE)
        == TALK_TEMPLATE
        != template_for_target_namespace(USER_NAMESPACE)
    )


def test_namespace_with_no_defined_rcat_returns_none():
    assert template_for_target_namespace(6) is None  # File


def test_xnr_tagged_categories_mirrors_the_sql_whitelist():
    # Mirrors untagged_redirects_querry.sql's lt.lt_title IN (...) list
    # exactly: one category per TARGET_NAMESPACE_TEMPLATES entry, one for
    # TALK_TEMPLATE, plus the AfC exception -- 11 total, matching the SQL.
    assert len(XNR_TAGGED_CATEGORIES) == len(TARGET_NAMESPACE_TEMPLATES) + 2
    assert TALK_TEMPLATE[1] in XNR_TAGGED_CATEGORIES
    assert AFC_DRAFT_CATEGORY in XNR_TAGGED_CATEGORIES
    for _, category in TARGET_NAMESPACE_TEMPLATES.values():
        assert category in XNR_TAGGED_CATEGORIES


def test_shell_aliases():
    assert all(name == name.lower() for name in SHELL_ALIASES)
    assert SHELL_CANONICAL_NAME.lower() in SHELL_ALIASES
    for alias in ("rcat shell", "rcat", "redr", "r cat shell"):
        assert alias in SHELL_ALIASES
    # not to be confused with an ordinary rcat, which the shell just wraps
    assert "r to category namespace" not in SHELL_ALIASES


# --- scope_violation ----------------------------------------------------------

OUT_OF_SCOPE_PAIRS = [
    (MAIN_NAMESPACE, MAIN_NAMESPACE),  # same namespace
    # All three EXCLUDED_SOURCE_NAMESPACES members, checked individually --
    # a WP:BOTPOLICY-relevant exclusion, not an arbitrary set to sample from.
    (USER_NAMESPACE, MAIN_NAMESPACE),
    (USER_TALK_NAMESPACE, MAIN_NAMESPACE),
    (DRAFT_NAMESPACE, MAIN_NAMESPACE),
    (1, 5),  # Talk -> Wikipedia talk (talk-to-talk)
    (MOS_NAMESPACE, WIKIPEDIA_NAMESPACE),  # legacy pseudo-namespace pattern
]

IN_SCOPE_PAIRS = [
    (WIKIPEDIA_NAMESPACE, USER_NAMESPACE),  # User: is a valid *target*, just not a valid source
    (5, WIKIPEDIA_NAMESPACE),  # Wikipedia talk -> Wikipedia (talk -> non-talk is fine)
    (MOS_NAMESPACE, HELP_NAMESPACE),  # MOS -> Help is fine (only MOS -> Wikipedia is excluded)
    # Draft talk (119, no named constant) is NOT excluded, only Draft itself
    # -- easy to get wrong by assuming the exclusion covers the whole
    # Draft/Draft talk pair. Real example: sample_candidates_enwiki.csv's
    # Draft talk -> Draft "Tom & Jerry" row.
    (119, DRAFT_NAMESPACE),
    (-1, MAIN_NAMESPACE),  # negative namespaces don't crash the talk-namespace modulo check
    (MAIN_NAMESPACE, -1),
]


@pytest.mark.parametrize("source_ns, target_ns", OUT_OF_SCOPE_PAIRS)
def test_scope_violation_flags_out_of_scope_pairs(source_ns, target_ns):
    assert scope_violation(source_ns, target_ns) is not None


@pytest.mark.parametrize("source_ns, target_ns", IN_SCOPE_PAIRS)
def test_scope_violation_allows_legitimate_pairs(source_ns, target_ns):
    assert scope_violation(source_ns, target_ns) is None


# --- is_r2_eligible ------------------------------------------------------------


@pytest.mark.parametrize("target_ns", [DRAFT_NAMESPACE, MOS_NAMESPACE])
def test_r2_eligible_for_mainspace_to_non_exempt_namespaces(target_ns):
    # R2 has no move exception (unlike R3) -- see policy.py's R2 section.
    # MOS: isn't among the five named exceptions either despite being newer.
    assert is_r2_eligible(MAIN_NAMESPACE, target_ns) is True


@pytest.mark.parametrize(
    "target_ns",
    [WIKIPEDIA_NAMESPACE, TEMPLATE_NAMESPACE, HELP_NAMESPACE, CATEGORY_NAMESPACE, PORTAL_NAMESPACE],
)
def test_r2_exempt_for_the_five_named_namespaces(target_ns):
    # High-stakes: getting even one of these five wrong either blocks
    # legitimate tagging or tags a redirect that should be speedy-deleted --
    # so all five are checked individually, unlike sampling elsewhere here.
    assert is_r2_eligible(MAIN_NAMESPACE, target_ns) is False


def test_r2_eligibility_only_applies_to_mainspace_sources():
    # Draft-sourced redirect to User: isn't an R2 matter at all
    assert is_r2_eligible(DRAFT_NAMESPACE, USER_NAMESPACE) is False


# --- add_rcat (golden files) ---------------------------------------------------
# Each case is a pair of files in wikitext_cases/: <name>_in.wikitext and
# <name>_out.wikitext -- open a pair to see exactly what changes.
#
# Worth reading even if you skip the rest: target_is_category_plus_trailing_
# category, where the redirect's own target is itself "[[Category:Foo]]" (our
# most common real case) *and* the page has a separate trailing content
# category -- a naive scan could mistake one for the other and corrupt the
# redirect.

WIKITEXT_TEMPLATE = "R to category namespace"
WIKITEXT_CASES_DIR = Path(__file__).parent / "wikitext_cases"
WIKITEXT_CASE_NAMES = sorted(
    {p.name.removesuffix("_in.wikitext") for p in WIKITEXT_CASES_DIR.glob("*_in.wikitext")}
)


@pytest.mark.parametrize("case_name", WIKITEXT_CASE_NAMES)
def test_add_rcat(case_name):
    input_text = (WIKITEXT_CASES_DIR / f"{case_name}_in.wikitext").read_text()
    expected = (WIKITEXT_CASES_DIR / f"{case_name}_out.wikitext").read_text()
    assert add_rcat(input_text, WIKITEXT_TEMPLATE) == expected