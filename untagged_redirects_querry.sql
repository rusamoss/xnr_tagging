-- Cross-namespace redirects (excluding Draft: -> Any namespace, User:, User talk:, File:, and MediaWiki: -> Any namespace, and any talk namespace -> any other talk namespace) NOT tagged with any XNR redirect category (e.g. Redirects to category space, Redirects to the main namespace), which have corresponding XNRs (so not e.g. redirects to the Module namespace, since {{R to module namespace}} doesn't exist).
-- Also excludes R2 eligible redirects (from mainspace to any namespace except Category:, Template:, Wikipedia:, Help:, and Portal:)

-- See https://quarry.wmcloud.org/query/107791 for XNRs that don't have corresponding rcat templates (e.g. to Module:).

-- Published at https://quarry.wmcloud.org/query/107774

SELECT
    src.page_title AS source_title,
    r.rd_title AS target_title,
    src.page_namespace AS source_ns,
    r.rd_namespace AS target_ns
FROM redirect r
INNER JOIN page src
        ON src.page_id = r.rd_from
LEFT JOIN linktarget lt
        ON lt.lt_namespace = 14
       AND lt.lt_title IN (
            'Redirects_to_talk_pages',
            'Redirects_to_user_namespace',
            'Redirects_to_project_namespace',
            'Redirects_to_template_namespace',
            'Redirects_to_help_namespace',
            'Redirects_to_category_space',
            'Redirects_to_portal_namespace',
            'Redirects_to_the_draft_namespace',
            'Redirects_to_MOS_namespace',
            'Redirects_from_old_AfC_drafts',
            'Redirects_to_the_main_namespace'
       )
LEFT JOIN categorylinks cl
        ON cl.cl_target_id = lt.lt_id
       AND cl.cl_type      = 'page'
       AND cl.cl_from      = src.page_id
WHERE src.page_namespace != r.rd_namespace
  AND (r.rd_interwiki = '' OR r.rd_interwiki IS NULL) -- not interwiki link
  AND src.page_namespace NOT IN (2, 3, 6, 8, 118) -- not from User, User talk, File, MediaWiki, Draft
  -- File (6) and MediaWiki (8) added 2026-09-04: Rusabot can never save to these regardless of
  -- target -- confirmed live via "noimageredirect" (File: redirects) and "protectednamespace-interface"
  -- (MediaWiki: needs editinterface) API errors. TODO: revisit File: if Rusabot's permissions ever
  -- gain image-redirect rights; the backlog there is real, just untaggable by this account today.
  AND NOT (src.page_namespace % 2 = 1 AND r.rd_namespace % 2 = 1) -- avoid talk -> talk
  AND NOT (src.page_namespace = 126 AND r.rd_namespace = 4) -- avoid MOS -> Wikipedia
  AND NOT (src.page_namespace = 0 AND r.rd_namespace NOT IN (4, 10, 12, 14, 100)) -- avoid R2-eligible mainspace XNRs (mainspace -> anything except Wikipedia, Template, Help, Category, Portal)
  AND (
        r.rd_namespace % 2 = 1                          -- to any talk namespace
     OR r.rd_namespace IN (0, 2, 4, 10, 12, 14, 100, 118, 126)  -- to Main, User, Wikipedia, Template, Help, Category, Portal, Draft, MOS (with rcats)
      )
GROUP BY src.page_id, src.page_title, src.page_namespace, r.rd_namespace, r.rd_title
HAVING SUM(CASE WHEN cl.cl_from IS NOT NULL THEN 1 ELSE 0 END) = 0
ORDER BY src.page_namespace, r.rd_namespace, src.page_title;
