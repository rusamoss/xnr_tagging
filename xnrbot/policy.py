"""Wikipedia-side policy knowledge -- which rcat template applies to a target
namespace, which (source, target) pairs are legitimate XNR candidates at
all, which are actually WP:CSD#R2 speedy-deletion candidates rather than
tagging candidates -- and, at the bottom of this module, add_rcat(), which
applies that knowledge to actual wikitext per WP:REDCAT.
"""
from __future__ import annotations

import re

import mwparserfromhell
from mwparserfromhell.nodes import Template, Wikilink

# --- Template selection --------------------------------------------------
# Namespace IDs here must stay a subset of the target-namespace whitelist in
# ../untagged_redirects_querry.sql -- that query is what decides which
# namespaces are in scope in the first place.

MAIN_NAMESPACE = 0
USER_NAMESPACE = 2
USER_TALK_NAMESPACE = 3
WIKIPEDIA_NAMESPACE = 4
FILE_NAMESPACE = 6
MEDIAWIKI_NAMESPACE = 8
TEMPLATE_NAMESPACE = 10
HELP_NAMESPACE = 12
CATEGORY_NAMESPACE = 14
PORTAL_NAMESPACE = 100
DRAFT_NAMESPACE = 118
MOS_NAMESPACE = 126

# target namespace ID -> (rcat template name, category it populates when the
# redirect's source is outside that namespace)
TARGET_NAMESPACE_TEMPLATES: dict[int, tuple[str, str]] = {
    MAIN_NAMESPACE: ("R to main namespace", "Redirects to the main namespace"),
    USER_NAMESPACE: ("R to user namespace", "Redirects to user namespace"),
    WIKIPEDIA_NAMESPACE: ("R to project namespace", "Redirects to project namespace"),
    TEMPLATE_NAMESPACE: ("R to template namespace", "Redirects to template namespace"),
    HELP_NAMESPACE: ("R to help namespace", "Redirects to help namespace"),
    CATEGORY_NAMESPACE: ("R to category namespace", "Redirects to category space"),
    PORTAL_NAMESPACE: ("R to portal namespace", "Redirects to portal namespace"),
    DRAFT_NAMESPACE: ("R to draft namespace", "Redirects to the draft namespace"),
    MOS_NAMESPACE: ("R to MOS namespace", "Redirects to MOS namespace"),
}

# Any odd-numbered namespace is a talk namespace and uses the same template
TALK_TEMPLATE: tuple[str, str] = ("R to talk page", "Redirects to talk pages")


def is_talk_namespace(namespace_id: int) -> bool:
    """Whether namespace_id is a talk namespace (odd-numbered, non-negative)."""
    return namespace_id >= 0 and namespace_id % 2 == 1


def template_for_target_namespace(namespace_id: int) -> tuple[str, str] | None:
    """Return (template_name, category_name) for a redirect landing in
    namespace_id, or None if no XNR rcat template is defined for it."""
    if is_talk_namespace(namespace_id):
        return TALK_TEMPLATE
    return TARGET_NAMESPACE_TEMPLATES.get(namespace_id)


# Old AfC (Articles for Creation) drafts lived on the submission's talk page
# (Wikipedia talk:Articles for creation/X); once accepted, that page was
# moved to mainspace, leaving a Wikipedia talk -> Main redirect behind.
AFC_DRAFT_CATEGORY = "Redirects from old AfC drafts"

# Mirrors ../untagged_redirects_querry.sql's lt.lt_title whitelist exactly:
# the query excludes a redirect carrying *any* of these, not just the one
# matching its target namespace, so the live re-check must test the same
# full set or it'll drift from what the query already considers tagged.
XNR_TAGGED_CATEGORIES: frozenset[str] = frozenset(
    {category for _, category in TARGET_NAMESPACE_TEMPLATES.values()}
    | {TALK_TEMPLATE[1], AFC_DRAFT_CATEGORY}
)

RFD_TRACKING_CATEGORY = "All redirects for discussion"
CSD_TRACKING_CATEGORY = "Candidates for speedy deletion"

SHELL_CANONICAL_NAME = "Redirect category shell"

# Aliases for Template:Redirect category shell
SHELL_ALIASES: frozenset[str] = frozenset(
    name.lower()
    for name in [
        "Redirect category shell",
        "This is a redirect",
        "This is a redirect.",
        "Redr",
        "REDR",
        "Rcat",
        "RCAT",
        "R cat",
        "Rcat shell",
        "RCAT shell",
        "RCat shell",
        "RCat Shell",
        "Rcat Shell",
        "Redirect shell",
        "Rcatsh",
        "Rcatshell",
        "RCATSHELL",
        "R shell",
        "R cat shell",
        "Rshell",
        "R category shell",
        "Redirect banner shell",
        "Rcat banner holder",
        "Redirect category holder",
        "Rcatholder",
        "Rcatgroup",
        "Redirect category group",
        "Redirect cat shell",
        "R cs",
        "RCS",
        "Rcs",
        "R catsh",
        "Red shell",
        "Redirect container",
        "Redirect Category shell",
        "REDIRECT CATEGORY SHELL",
        "Redirect reason",
        "Redirect reasons",
    ]
)


# --- Scope guard -----------------------------------------------------------
# Mirrors the exclusion rules in ../untagged_redirects_querry.sql's WHERE
# clause -- defense-in-depth for --candidates-file, which bypasses that
# query entirely.

def scope_violation(source_namespace: int, target_namespace: int) -> str | None:
    """Return a human-readable reason this (source, target) pair is out of
    scope, or None if it's a legitimate XNR candidate."""
    if source_namespace == target_namespace:
        return "source and target are in the same namespace"

    # Source namespaces the query excludes entirely, regardless of target.
    # File and MediaWiki are excluded because Rusabot categorically can't
    # save to them, not because the redirects themselves are out of scope --
    # confirmed live 2026-09-04: MediaWiki API rejects every save attempt
    # with "noimageredirect" (any edit to File: content that's still a
    # redirect needs a right this bot doesn't have, re-checked on every
    # save, not just creation) or "protectednamespace-interface" (MediaWiki:
    # needs editinterface, admin-only). TODO: revisit excluding File: if/when
    # Rusabot's permissions are regenerated with the right to create/edit
    # image redirects -- the backlog of File:-sourced XNRs is real, just
    # untaggable by this bot account today.
    EXCLUDED_SOURCE_NAMESPACES = frozenset(
        {USER_NAMESPACE, USER_TALK_NAMESPACE, FILE_NAMESPACE, MEDIAWIKI_NAMESPACE, DRAFT_NAMESPACE}
    )
    if source_namespace in EXCLUDED_SOURCE_NAMESPACES:
        return "source namespace is excluded (User, User talk, File, MediaWiki, or Draft)"

    if is_talk_namespace(source_namespace) and is_talk_namespace(target_namespace):
        return "talk-to-talk redirects are out of scope"

    if source_namespace == MOS_NAMESPACE and target_namespace == WIKIPEDIA_NAMESPACE:
        return "MOS-to-Wikipedia redirects are out of scope"

    return None


# --- WP:CSD#R2 -------------------------------------------------------------
# The five namespaces R2 exempts from "mainspace redirects to any other
# namespace are speedy-deletable".
def is_r2_eligible(source_namespace: int, target_namespace: int) -> bool:
    """Whether this pair is a WP:CSD#R2 speedy-deletion candidate.

    Also enforced in ../untagged_redirects_querry.sql -- this is the
    defense-in-depth copy for --candidates-file, which bypasses that query.
    """
    R2_EXEMPT_TARGET_NAMESPACES = frozenset(
        {WIKIPEDIA_NAMESPACE, TEMPLATE_NAMESPACE, CATEGORY_NAMESPACE, HELP_NAMESPACE, PORTAL_NAMESPACE}
    )
    return source_namespace == MAIN_NAMESPACE and target_namespace not in R2_EXEMPT_TARGET_NAMESPACES


# --- Applying an rcat to wikitext -------------------------------------------
# Follows WP:REDCAT: rcats go before content categories/{{DEFAULTSORT:}}, not
# after. add_rcat() touches nothing else on the page -- it only ever adds one
# rcat inside an existing shell, or one new {{Redirect category shell| {{rcat}} }} block.

def _normalize(name) -> str:
    return str(name).strip().replace("_", " ").lower()


def _find_shell_template(wikicode):
    for template in wikicode.filter_templates(recursive=True):
        if _normalize(template.name) in SHELL_ALIASES:
            return template
    return None


def _find_content_category_insertion_index(wikicode) -> int | None:
    """Index in wikicode.nodes of the first trailing content category link
    or {{DEFAULTSORT:}}, or None if there isn't one.
    """
    nodes = wikicode.nodes
    start = 0
    for i, node in enumerate(nodes):
        if isinstance(node, Wikilink):
            start = i + 1
            break

    for i in range(start, len(nodes)):
        node = nodes[i]
        if isinstance(node, Wikilink) and str(node.title).strip().lower().startswith("category:"):
            return i
        if isinstance(node, Template) and _normalize(node.name).startswith("defaultsort:"):
            return i
    return None


def _has_template(wikicode, template_name: str) -> bool:
    target = _normalize(template_name)
    return any(_normalize(t.name) == target for t in wikicode.filter_templates(recursive=True))


def _is_bare_rcat(template) -> bool:
    """Whether `template` looks like a redirect-category ("Rcat") template by
    name, per Wikipedia's near-universal "R from X" / "R to X" / "R with X"
    naming convention (confirmed against Wikipedia:Template_index/Redirect_pages
    -- the shell itself and its aliases are excluded separately via
    SHELL_ALIASES and never reach this check, since it's only called once no
    shell has been found on the page). Name-based, not a category-membership
    check -- a non-rcat template that happened to be named "R something"
    would be misdetected, but no real example of that has turned up.
    """
    return _normalize(template.name).startswith("r ")


def add_rcat(text: str, template_name: str) -> str:
    """Return page content with {{template_name}} added as an XNR rcat tag.

    If the page has a redirect category shell, the template is appended inside it.
    Otherwise a new shell is inserted -- sweeping in any bare (unshelled) rcats
    already on the page (see _is_bare_rcat) rather than leaving them stranded
    next to a shell that only has the one new template, since WP:REDCAT expects
    a page's rcats consolidated under a single shell.

    Unchanged if template_name is already present anywhere (defensive
    no-op; callers should already have checked via live category
    membership).
    """
    wikicode = mwparserfromhell.parse(text)
    if _has_template(wikicode, template_name):
        return text

    new_rcat = f"{{{{{template_name}}}}}"

    shell = _find_shell_template(wikicode)
    if shell is not None:
        if shell.has("1"):
            param = shell.get("1")
            existing = str(param.value)
            separator = "" if existing.endswith("\n") else "\n"
            param.value = f"{existing}{separator}{new_rcat}\n"
        else:
            shell.add("1", f"\n{new_rcat}\n")
        return str(wikicode)

    bare_rcats = [t for t in wikicode.filter_templates(recursive=False) if _is_bare_rcat(t)]
    for template in bare_rcats:
        wikicode.remove(template)
    shelled = "\n".join([str(t) for t in bare_rcats] + [new_rcat])
    block = f"{{{{{SHELL_CANONICAL_NAME}|\n{shelled}\n}}}}"

    insertion_index = _find_content_category_insertion_index(wikicode)

    if insertion_index is None:
        stripped = str(wikicode).rstrip()
        result = f"{stripped}\n\n{block}\n"
    else:
        nodes = wikicode.nodes
        before = "".join(str(n) for n in nodes[:insertion_index]).rstrip()
        after = "".join(str(n) for n in nodes[insertion_index:])
        result = f"{before}\n\n{block}\n\n{after}"

    # Removing a bare rcat leaves its blank-line padding behind; collapse the
    # resulting run of blank lines rather than tracking exact whitespace
    # through node removal.
    return re.sub(r"\n{3,}", "\n\n", result)
