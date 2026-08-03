-- Reconstruct the full path a request took across the multi-agent system.
--
-- Paste into AlloyDB Studio and edit the single value in `params` below.
--
-- WHY THIS NEEDS A JOIN AT ALL
-- Each agent mints its OWN session: the A2A callee generates a context_id and
-- ADK uses it as the session id (adk/a2a/converters/request_converter.py). So
-- the orchestrator's session and math's session are different rows, in
-- different schemas, with no shared key on the sessions table itself.
--
-- The link lives in the CALLER's event metadata. When a RemoteA2aAgent gets a
-- response it writes the REMOTE task/context onto its own event:
--     event_data -> 'custom_metadata' ->> 'a2a:context_id'  = callee's session id
--     event_data -> 'custom_metadata' ->> 'a2a:task_id'     = callee's task id
-- That is the edge this query walks, recursively, so it handles delegation
-- chains of any depth (orchestrator -> research -> ...), not just one hop.
--
-- This cross-schema walk is precisely what a database-per-agent layout would
-- have made impossible: PostgreSQL cannot join across databases.

WITH RECURSIVE
params AS (
    -- START HERE. Any of these work; leave the others NULL.
    SELECT
        NULL::text AS root_session,   -- e.g. '4be80423-bfb9-4078-8d37-b82b6661533d'
        NULL::text AS root_task,      -- e.g. '3a226bc1-31d1-4bf1-8dc8-16e9e739b0cc'
        NULL::text AS search_text     -- e.g. '%17 * 23%'  (finds the session for you)
),

-- Add a line to each of the three UNIONs when you add an agent.
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

-- Resolve whichever entry point was supplied down to one root session id.
root AS (
    SELECT s.agent, s.id AS session_id
    FROM all_sessions s, params p
    WHERE s.id = p.root_session
       OR s.id = (SELECT t.context_id FROM all_tasks t WHERE t.id = p.root_task)
       OR (p.search_text IS NOT NULL AND s.id IN (
              SELECT e.session_id FROM all_events e
              WHERE e.event_data #>> '{content,parts,0,text}' LIKE p.search_text
          ))
),

-- Delegation edges: a caller event carrying the callee's context id.
hops AS (
    SELECT
        e.agent                                            AS caller_agent,
        e.session_id                                       AS caller_session,
        e.event_data ->> 'author'                          AS callee_agent,
        e.event_data -> 'custom_metadata' ->> 'a2a:context_id' AS callee_session,
        e.event_data -> 'custom_metadata' ->> 'a2a:task_id'    AS callee_task
    FROM all_events e
    WHERE e.event_data -> 'custom_metadata' ? 'a2a:context_id'
),

-- Walk the tree. cycle detection guards against a malformed metadata loop.
walk AS (
    -- The ::text casts are required, not cosmetic: sessions.id is
    -- varchar(128) but the recursive term produces text (from ->>), and
    -- PostgreSQL rejects a recursive CTE whose two terms disagree on type.
    SELECT r.agent, r.session_id::text AS session_id, 0 AS depth,
           ARRAY[r.session_id::text] AS seen
    FROM root r
  UNION ALL
    SELECT h.callee_agent, h.callee_session, w.depth + 1,
           w.seen || h.callee_session
    FROM walk w
    JOIN hops h ON h.caller_session = w.session_id
    WHERE NOT h.callee_session = ANY(w.seen)
)

SELECT
    w.depth,
    repeat('    ', w.depth) || w.agent                       AS agent,
    e.timestamp,
    e.event_data ->> 'author'                                AS author,
    -- One readable line per event, whatever kind it is.
    CASE
        WHEN e.event_data -> 'custom_metadata' ? 'a2a:context_id'
            THEN '<= A2A response from '
                 || (e.event_data ->> 'author')
                 || ' (task ' || left(e.event_data -> 'custom_metadata'
                                      ->> 'a2a:task_id', 8) || ')'
        WHEN jsonb_path_exists(e.event_data, '$.content.parts[*].function_call')
            THEN '-> call '
                 || (jsonb_path_query_first(e.event_data,
                        '$.content.parts[*].function_call.name') #>> '{}')
                 || coalesce('(' || (jsonb_path_query_first(e.event_data,
                        '$.content.parts[*].function_call.args') :: text) || ')', '')
        WHEN jsonb_path_exists(e.event_data, '$.content.parts[*].function_response')
            THEN '<- '
                 || (jsonb_path_query_first(e.event_data,
                        '$.content.parts[*].function_response.name') #>> '{}')
                 || ' returned '
                 || left(coalesce(jsonb_path_query_first(e.event_data,
                        '$.content.parts[*].function_response.response') :: text, ''), 90)
        ELSE left(coalesce(
                 jsonb_path_query_first(e.event_data,
                     '$.content.parts[*].text') #>> '{}', ''), 110)
    END                                                      AS what,
    e.session_id,
    e.invocation_id
FROM walk w
JOIN all_events e
  ON e.agent = w.agent AND e.session_id = w.session_id
ORDER BY e.timestamp, w.depth;
