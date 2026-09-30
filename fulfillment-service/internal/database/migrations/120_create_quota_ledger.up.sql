-- Tenant quota configuration and transactional usage ledger.

create table quota_default_limits (
  dimension text not null check (dimension <> ''),
  class_key text not null default '',
  limit_value bigint not null check (limit_value >= 0),
  updated_at timestamptz not null default now(),
  primary key (dimension, class_key)
);

create table tenant_quota_limits (
  tenant text not null references tenants (name) on delete cascade,
  dimension text not null check (dimension <> ''),
  class_key text not null default '',
  limit_value bigint not null check (limit_value >= 0),
  warning_threshold bigint check (warning_threshold >= 0),
  updated_at timestamptz not null default now(),
  primary key (tenant, dimension, class_key)
);

create index tenant_quota_limits_by_tenant on tenant_quota_limits (tenant);

create table quota_usage (
  tenant text not null references tenants (name) on delete cascade,
  dimension text not null check (dimension <> ''),
  class_key text not null default '',
  used bigint not null default 0 check (used >= 0),
  updated_at timestamptz not null default now(),
  primary key (tenant, dimension, class_key)
);

create table quota_claims (
  claim_id uuid not null primary key default uuidv7(),
  tenant text not null references tenants (name),
  owner_type text not null check (owner_type <> ''),
  owner_id text not null check (owner_id <> ''),
  charge_path text not null check (charge_path <> ''),
  dimension text not null check (dimension <> ''),
  class_key text not null default '',
  units bigint not null check (units > 0),
  transfer_key text,
  created_at timestamptz not null default now(),
  unique (tenant, owner_type, owner_id, charge_path, dimension, class_key)
);

create index quota_claims_by_tenant_dimension
  on quota_claims (tenant, dimension, class_key);

create unique index quota_claims_by_transfer_key
  on quota_claims (tenant, transfer_key)
  where transfer_key is not null;

create function apply_quota_usage_delta(
  p_tenant text,
  p_dimension text,
  p_class_key text,
  p_delta bigint
) returns void as $$
begin
  if p_delta = 0 then
    return;
  end if;

  if p_delta > 0 then
    insert into quota_usage (tenant, dimension, class_key, used)
    values (p_tenant, p_dimension, p_class_key, p_delta)
    on conflict (tenant, dimension, class_key) do update
      set used = quota_usage.used + excluded.used,
          updated_at = now();
    return;
  end if;

  update quota_usage
  set used = used + p_delta,
      updated_at = now()
  where tenant = p_tenant
    and dimension = p_dimension
    and class_key = p_class_key
    and used >= -p_delta;

  if not found then
    raise exception 'quota usage projection is inconsistent for tenant %, dimension %, class %',
      p_tenant, p_dimension, p_class_key
      using errcode = '23514';
  end if;
end;
$$ language plpgsql;

create function project_quota_claim() returns trigger as $$
begin
  if TG_OP = 'INSERT' then
    perform apply_quota_usage_delta(new.tenant, new.dimension, new.class_key, new.units);
    return new;
  elsif TG_OP = 'DELETE' then
    perform apply_quota_usage_delta(old.tenant, old.dimension, old.class_key, -old.units);
    return old;
  end if;

  if old.tenant = new.tenant
    and old.dimension = new.dimension
    and old.class_key = new.class_key then
    perform apply_quota_usage_delta(
      new.tenant,
      new.dimension,
      new.class_key,
      new.units - old.units
    );
  else
    perform apply_quota_usage_delta(old.tenant, old.dimension, old.class_key, -old.units);
    perform apply_quota_usage_delta(new.tenant, new.dimension, new.class_key, new.units);
  end if;
  return new;
end;
$$ language plpgsql;

create trigger project_quota_claim
  after insert or update or delete on quota_claims
  for each row execute function project_quota_claim();

create function copy_quota_defaults_to_tenant() returns trigger as $$
begin
  insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
  select new.name, dimension, class_key, limit_value
  from quota_default_limits
  on conflict (tenant, dimension, class_key) do nothing;
  return new;
end;
$$ language plpgsql;

create trigger copy_quota_defaults_to_tenant
  after insert on tenants
  for each row execute function copy_quota_defaults_to_tenant();

create table quota_limit_audit (
  id uuid not null primary key default uuidv7(),
  tenant text,
  dimension text not null,
  class_key text not null default '',
  previous_limit bigint,
  new_limit bigint not null check (new_limit >= 0),
  actor text not null,
  changed_at timestamptz not null default now()
);

create index quota_limit_audit_by_tenant_time
  on quota_limit_audit (tenant, changed_at desc);
