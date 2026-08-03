-- One row per delegation hop: who called whom, and what it cost.
--
-- The compact companion to request_path.sql -- use this when you want the shape
-- of a request rather than its full event stream. Leave `root_session` NULL to
-- see every hop in the system.
--
-- Each hop joins the caller's event metadata to the callee's task and session,
-- so the per-agent counters (events, tokens, state) come from the callee's own
-- schema. That is the join a database-per-agent layout would have prevented.

WITH
params AS (
    SELECT NULL::text AS root_session   -- e.g. '4be80423-bfb9-4078-8d37-b82b6661533d'
),

all_events AS (
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.events
    UNION ALL SELECT 'research'::text, * FROM research.events
    UNION ALL SELECT 'math'::text,     * FROM math.events
),
all_sessions AS (
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.sessions
    UNION ALL SELECT 'research'::text, * FROM research.sessions
    UNION ALL SELECT 'math'::text,     * FROM math.sessions
),
all_tasks AS (
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.tasks
    UNION ALL SELECT 'research'::text, * FROM research.tasks
    UNION ALL SELECT 'math'::text,     * FROM math.tasks
),

hops AS (
    SELECT
        e.agent                                                     AS caller_agent,
        e.session_id::text                                          AS caller_session,
        e.event_data ->> 'author'                                   AS callee_agent,
        e.event_data -> 'custom_metadata' ->> 'a2a:context_id'      AS callee_session,
        e.event_data -> 'custom_metadata' ->> 'a2a:task_id'         AS callee_task,
        e.timestamp                                                 AS returned_at,
        -- ADK stamps the callee's own invocation id into the response metadata;
        -- it is the handle for correlating with that agent's logs and traces.
        e.event_data -> 'custom_metadata' -> 'a2a:response' -> 'metadata'
            ->> 'adk_invocation_id'                                 AS callee_invocation,
        (e.event_data -> 'custom_metadata' -> 'a2a:response' -> 'metadata'
            -> 'adk_usage_metadata' ->> 'totalTokenCount')::int      AS callee_tokens
    FROM all_events e
    WHERE e.event_data -> 'custom_metadata' ? 'a2a:context_id'
)

SELECT
    h.caller_agent || ' -> ' || h.callee_agent   AS hop,
    h.returned_at,
    t.status ->> 'state'                         AS callee_task_state,
    t.updated_at - t.created_at                  AS callee_duration,
    h.callee_tokens,
    (SELECT count(*) FROM all_events e2
      WHERE e2.session_id = h.callee_session)    AS callee_events,
    s.state                                      AS callee_session_state,
    h.callee_session,
    h.callee_task,
    h.callee_invocation
FROM hops h
LEFT JOIN all_tasks    t ON t.id = h.callee_task
LEFT JOIN all_sessions s ON s.id = h.callee_session
-- params is referenced as a scalar subquery, not comma-joined: mixing a comma
-- join with LEFT JOIN binds the LEFT JOIN to `params` instead of `hops`, which
-- PostgreSQL rejects with "invalid reference to FROM-clause entry".
WHERE (SELECT root_session FROM params) IS NULL
   OR h.caller_session = (SELECT root_session FROM params)
ORDER BY h.returned_at DESC;
