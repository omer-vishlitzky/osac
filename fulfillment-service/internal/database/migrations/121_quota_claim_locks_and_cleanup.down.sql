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
    execute format('drop trigger if exists release_quota_claims on %I', tbl);
  end loop;
end;
$$;

drop function release_quota_claims_for_object();

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
  insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
  select new.name, dimension, class_key, limit_value
  from quota_default_limits
  on conflict (tenant, dimension, class_key) do nothing;
  return new;
end;
$$ language plpgsql;

drop function quota_lock_key(text, text, text, text);
