create function quota_lock_key(
  p_scope text,
  p_tenant text,
  p_dimension text,
  p_class_key text
) returns bigint as $$
  select hashtextextended(
    length(p_scope)::text || ':' || p_scope ||
    length(p_tenant)::text || ':' || p_tenant ||
    length(p_dimension)::text || ':' || p_dimension ||
    length(p_class_key)::text || ':' || p_class_key,
    0
  );
$$ language sql immutable strict;

create or replace function apply_quota_usage_delta(
  p_tenant text,
  p_dimension text,
  p_class_key text,
  p_delta bigint
) returns void as $$
begin
  if p_delta = 0 then
    return;
  end if;

  perform pg_advisory_xact_lock(quota_lock_key('usage', p_tenant, p_dimension, p_class_key));

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

create or replace function copy_quota_defaults_to_tenant() returns trigger as $$
begin
  -- Prevent default changes during the snapshot while allowing concurrent
  -- tenant creations to copy the same defaults.
  lock table quota_default_limits in share mode;
  insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
  select new.name, dimension, class_key, limit_value
  from quota_default_limits
  order by dimension, class_key;
  return new;
end;
$$ language plpgsql;

create function release_quota_claims_for_object() returns trigger as $$
begin
  delete from quota_claims
  where tenant = old.tenant
    and owner_type = TG_TABLE_NAME
    and owner_id = old.id;
  return old;
end;
$$ language plpgsql;

do $$
declare
  tbl text;
begin
  foreach tbl in array array[
    'bare_metal_instances',
    'clusters',
    'compute_instances',
    'disk_images',
    'external_ips',
    'nat_gateways',
    'virtual_networks',
    'volumes'
  ]
  loop
    execute format(
      'create trigger release_quota_claims
         after delete on %I
         for each row execute function release_quota_claims_for_object()',
      tbl
    );
  end loop;
end;
$$;
