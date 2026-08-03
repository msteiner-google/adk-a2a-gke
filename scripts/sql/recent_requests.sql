-- Recent requests, newest first -- the entry point for request_path.sql.
--
-- One row per A2A task, with the agent that handled it, how many agents it
-- fanned out to, and how long it took. Copy a `session_id` from here into the
-- `params` block of request_path.sql to see the full timeline.
--
-- `duration` is only meaningful because this repo adds created_at/updated_at to
-- the tasks table -- a2a's own TaskMixin has no timestamps at all. The scan is
-- served by idx_tasks_updated_at.

WITH
all_events AS (
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.events
    UNION ALL SELECT 'research'::text, * FROM research.events
    UNION ALL SELECT 'math'::text,     * FROM math.events
),
all_tasks AS (
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.tasks
    UNION ALL SELECT 'research'::text, * FROM research.tasks
    UNION ALL SELECT 'math'::text,     * FROM math.tasks
),

-- Sessions that were reached over A2A, i.e. someone else's callee. Anything
-- NOT in here was entered directly by a user, so it is a root request.
delegated_sessions AS (
    SELECT DISTINCT e.event_data -> 'custom_metadata' ->> 'a2a:context_id' AS session_id
    FROM all_events e
    WHERE e.event_data -> 'custom_metadata' ? 'a2a:context_id'
),

-- Which agents each root request fanned out to.
fanout AS (
    SELECT e.session_id,
           array_agg(DISTINCT e.event_data ->> 'author') AS delegated_to
    FROM all_events e
    WHERE e.event_data -> 'custom_metadata' ? 'a2a:context_id'
    GROUP BY e.session_id
)

SELECT
    t.updated_at                                    AS finished_at,
    t.agent                                         AS entry_agent,
    t.context_id                                    AS session_id,  -- <- for request_path.sql
    t.id                                            AS task_id,
    t.status ->> 'state'                            AS state,
    t.updated_at - t.created_at                     AS duration,
    coalesce(f.delegated_to, ARRAY[]::text[])       AS delegated_to,
    jsonb_array_length(t.history::jsonb)            AS messages,
    left(coalesce(
        t.history::jsonb #>> '{0,parts,0,text}', ''), 80)  AS first_message
FROM all_tasks t
LEFT JOIN fanout f ON f.session_id = t.context_id
-- NOT EXISTS rather than NOT IN: a single NULL in the subquery would make
-- `NOT IN` evaluate to NULL for every row and silently return nothing.
WHERE NOT EXISTS (
    SELECT 1 FROM delegated_sessions d WHERE d.session_id = t.context_id
)
ORDER BY t.updated_at DESC
LIMIT 25;
