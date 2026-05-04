-- Catalog database schema for the in-app Sessions Manager.
-- Mirrors vendor/CodexMM/src/server/services/session-repository.ts so the
-- legacy Node service remains able to read this database during a rollback.

create table if not exists sessions (
  id text primary key,
  active_path text,
  archive_path text,
  snapshot_path text,
  original_relative_path text,
  cwd text not null,
  started_at text not null,
  originator text not null,
  source text not null,
  cli_version text not null,
  model_provider text not null,
  size_bytes integer not null default 0,
  line_count integer not null default 0,
  event_count integer not null default 0,
  tool_call_count integer not null default 0,
  user_prompt_excerpt text not null default '',
  latest_agent_message_excerpt text not null default '',
  status text not null,
  created_at text not null,
  updated_at text not null,
  indexed_at text not null,
  primary_mtime_ns integer not null default 0,
  parser_version integer not null default 0
);

create table if not exists timeline_items (
  session_id text not null,
  ordinal integer not null,
  item_id text not null,
  type text not null,
  timestamp text not null,
  text text,
  tool_name text,
  summary text,
  input_text text,
  output_text text,
  status text,
  attachments_json text,
  primary key (session_id, ordinal)
);

create table if not exists audit_log (
  id integer primary key autoincrement,
  action text not null,
  session_id text not null,
  source_path text,
  target_path text,
  details_json text not null default '{}',
  created_at text not null
);

create virtual table if not exists session_search using fts5(
  session_id UNINDEXED,
  id,
  cwd,
  user_prompt_excerpt,
  latest_agent_message_excerpt
);

create index if not exists idx_sessions_status_started_at
  on sessions(status, started_at desc);
create index if not exists idx_sessions_cwd_started_at
  on sessions(cwd, started_at desc);
create index if not exists idx_sessions_started_at
  on sessions(started_at desc);
create index if not exists idx_timeline_items_session_ordinal
  on timeline_items(session_id, ordinal asc);
create index if not exists idx_timeline_items_session_attachments
  on timeline_items(session_id, ordinal)
  where attachments_json is not null;
