create table quota_increase_requests (
  id text not null primary key,
  tenant text not null references tenants (name) on delete cascade,
  name text not null check (name <> ''),
  creator text not null,
  dimension text not null check (dimension <> ''),
  class_key text not null default '',
  requested_limit bigint not null check (requested_limit > 0),
  reason text not null check (reason <> ''),
  state text not null default 'PENDING' check (state in ('PENDING', 'APPROVED', 'DENIED')),
  review_message text not null default '',
  reviewed_by text not null default '',
  creation_timestamp timestamptz not null default now(),
  reviewed_at timestamptz,
  unique (tenant, name)
);

create index quota_increase_requests_by_tenant_created
  on quota_increase_requests (tenant, creation_timestamp desc);

create index quota_increase_requests_pending
  on quota_increase_requests (creation_timestamp)
  where state = 'PENDING';
