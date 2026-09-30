-- Seed the claim ledger from resources that already exist when quota admission
-- is enabled. CSI Volume rows own storage claims when present; otherwise the
-- ComputeInstance reserves the PVC's storage until CSI creates that row.
-- Rows with a deletion timestamp remain charged until their finalizers confirm
-- cleanup and the row is physically archived.

insert into quota_default_limits (dimension, class_key, limit_value)
values
  ('vcpus', '', 0),
  ('memory_gib', '', 0),
  ('gpus', '', 0),
  ('volumes', '', 0),
  ('storage_gib', '', 0),
  ('images', '', 0),
  ('external_ips', '', 0),
  ('nat_gateways', '', 0),
  ('virtual_networks', '', 0),
  ('bmaas_instances', '', 0),
  ('caas_control_planes', '', 0)
on conflict (dimension, class_key) do nothing;

-- Existing tenants predate the default-copy trigger; give them the zero
-- baseline too. New tenants receive the same rows from the trigger.
insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
select t.name, d.dimension, d.class_key, d.limit_value
from tenants t
cross join quota_default_limits d
where t.name not in ('shared', 'system')
on conflict (tenant, dimension, class_key) do nothing;

do $$
begin
  if exists (
    select 1
    from compute_instances ci
    left join instance_types it
      on it.id = ci.data->'spec'->'instance_type'->>'id'
    where ci.tenant not in ('shared', 'system')
      and coalesce(
        nullif(it.data->'spec'->>'vcpus', '')::bigint,
        nullif(it.data->'spec'->>'cores', '')::bigint,
        nullif(ci.data->'spec'->>'cores', '')::bigint
      ) is null
  ) then
    raise exception 'cannot backfill quota: a ComputeInstance has no resolvable vCPU class';
  end if;

  if exists (
    select 1
    from compute_instances ci
    left join instance_types it
      on it.id = ci.data->'spec'->'instance_type'->>'id'
    where ci.tenant not in ('shared', 'system')
      and coalesce(
        nullif(it.data->'spec'->>'memory_gib', '')::bigint,
        nullif(it.data->'spec'->>'memoryGib', '')::bigint,
        nullif(ci.data->'spec'->>'memory_gib', '')::bigint
      ) is null
  ) then
    raise exception 'cannot backfill quota: a ComputeInstance has no resolvable memory class';
  end if;

  if exists (
    select 1
    from volumes v
    where v.tenant not in ('shared', 'system')
      and (
        nullif(v.data->'spec'->>'size_gib', '') is null
        or coalesce(nullif(v.data->'spec'->>'storage_tier', ''), '') = ''
      )
  ) then
    raise exception 'cannot backfill quota: a Volume has no storage tier or size';
  end if;

  if exists (
    select 1
    from compute_instances ci
    cross join lateral (
      select ci.data->'spec'->'boot_disk' as disk, ci.name || '-root-disk' as pvc_name
      where jsonb_typeof(ci.data->'spec'->'boot_disk') = 'object'
      union all
      select disk, ci.name || '-disk-' || ordinal::text
      from jsonb_array_elements(coalesce(ci.data->'spec'->'additional_disks', '[]'::jsonb))
        with ordinality as disks(disk, ordinal)
    ) candidate
    left join storage_tiers st
      on st.id = candidate.disk->'storage_tier'->>'id'
    where ci.tenant not in ('shared', 'system')
      and (
        nullif(candidate.disk->>'size_gib', '') is null
        or coalesce(candidate.disk->'storage_tier'->>'name', st.name, candidate.disk->>'storage_tier', '') = ''
      )
  ) then
    raise exception 'cannot backfill quota: a ComputeInstance disk has no storage tier or size';
  end if;

  if exists (
    select 1
    from bare_metal_instances bmi
    left join bare_metal_instance_types bmit
      on bmit.id = bmi.data->'spec'->'instance_type'->>'id'
    left join bare_metal_instance_templates bit
      on bit.id = bmi.data->'spec'->'template'->>'id'
    where bmi.tenant not in ('shared', 'system')
      and bmit.id is null
      and nullif(bit.data->>'host_type', '') is null
  ) then
    raise exception 'cannot backfill quota: a BareMetalInstance has no resolvable hardware class';
  end if;

  if exists (
    select 1
    from clusters c
    cross join lateral jsonb_each(coalesce(c.data->'spec'->'node_sets', '{}'::jsonb)) as node_set
    where c.tenant not in ('shared', 'system')
      and coalesce(
        node_set.value->'baremetal_instance_type'->>'id',
        node_set.value->'baremetal_instance_type'->>'name',
        node_set.value->'host_type'->>'id',
        node_set.value->'host_type'->>'name'
      ) is null
  ) then
    raise exception 'cannot backfill quota: a Cluster node set has no hardware class';
  end if;
end;
$$;

-- Volumes claim both the volume count and the provisioned capacity.
insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select tenant, 'volumes', id, 'volume', 'volumes', '', 1, name || ':volumes'
from volumes
where tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select v.tenant,
       'volumes',
       v.id,
       'spec.size_gib',
       'storage_gib',
       v.data->'spec'->>'storage_tier',
       (v.data->'spec'->>'size_gib')::bigint,
       v.name || ':storage_gib'
from volumes v
where v.tenant not in ('shared', 'system')
on conflict do nothing;

-- Compute Instances claim CPU, memory, GPUs, and each disk's volume count and
-- capacity unless a CSI Volume row already owns those claims.
insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select ci.tenant,
       'compute_instances',
       ci.id,
       'spec.instance_type',
       'vcpus',
       '',
       coalesce(
         nullif(it.data->'spec'->>'vcpus', '')::bigint,
         nullif(it.data->'spec'->>'cores', '')::bigint,
         nullif(ci.data->'spec'->>'cores', '')::bigint
       ),
       null
from compute_instances ci
left join instance_types it
  on it.id = ci.data->'spec'->'instance_type'->>'id'
where ci.tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select ci.tenant,
       'compute_instances',
       ci.id,
       'spec.instance_type',
       'memory_gib',
       '',
       coalesce(
         nullif(it.data->'spec'->>'memory_gib', '')::bigint,
         nullif(it.data->'spec'->>'memoryGib', '')::bigint,
         nullif(ci.data->'spec'->>'memory_gib', '')::bigint
       ),
       null
from compute_instances ci
left join instance_types it
  on it.id = ci.data->'spec'->'instance_type'->>'id'
where ci.tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select ci.tenant,
       'compute_instances',
       ci.id,
       'spec.instance_type.gpu',
       'gpus',
       coalesce(it.data->'spec'->'gpu'->>'resource_name', it.data->'spec'->'gpu'->>'resourceName'),
       coalesce(
         nullif(it.data->'spec'->'gpu'->>'count', '')::bigint,
         nullif(it.data->'spec'->'gpu'->>'device_count', '')::bigint
       ),
       null
from compute_instances ci
join instance_types it
  on it.id = ci.data->'spec'->'instance_type'->>'id'
where ci.tenant not in ('shared', 'system')
  and coalesce(it.data->'spec'->'gpu'->>'resource_name', it.data->'spec'->'gpu'->>'resourceName') is not null
  and coalesce(
    nullif(it.data->'spec'->'gpu'->>'count', '')::bigint,
    nullif(it.data->'spec'->'gpu'->>'device_count', '')::bigint,
    0
  ) > 0
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'compute_instances', id, 'spec.auto_external_ip_attachment', 'external_ips', 1
from compute_instances
where tenant not in ('shared', 'system')
  and coalesce((data->'spec'->>'auto_external_ip_attachment')::boolean, false)
on conflict do nothing;

insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select ci.tenant,
       'compute_instances',
       ci.id,
       disk.path,
       'volumes',
       '',
       1,
       disk.pvc_name || ':volumes'
from compute_instances ci
cross join lateral (
  select ci.data->'spec'->'boot_disk' as disk,
         'spec.boot_disk' as path,
         ci.name || '-root-disk' as pvc_name
  where jsonb_typeof(ci.data->'spec'->'boot_disk') = 'object'
  union all
  select additional.disk,
         'spec.additional_disks[' || (additional.ordinal - 1)::text || ']' as path,
         ci.name || '-disk-' || additional.ordinal::text as pvc_name
  from jsonb_array_elements(coalesce(ci.data->'spec'->'additional_disks', '[]'::jsonb))
    with ordinality as additional(disk, ordinal)
) disk
where ci.tenant not in ('shared', 'system')
  and not exists (
    select 1
    from volumes v
    where v.tenant = ci.tenant
      and v.name = disk.pvc_name
  )
on conflict do nothing;

insert into quota_claims (
  tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
)
select ci.tenant,
       'compute_instances',
       ci.id,
       disk.path,
       'storage_gib',
       coalesce(disk.disk->'storage_tier'->>'name', st.name, disk.disk->>'storage_tier'),
       (disk.disk->>'size_gib')::bigint,
       disk.pvc_name || ':storage_gib'
from compute_instances ci
cross join lateral (
  select ci.data->'spec'->'boot_disk' as disk,
         'spec.boot_disk' as path,
         ci.name || '-root-disk' as pvc_name
  where jsonb_typeof(ci.data->'spec'->'boot_disk') = 'object'
  union all
  select additional.disk,
         'spec.additional_disks[' || (additional.ordinal - 1)::text || ']' as path,
         ci.name || '-disk-' || additional.ordinal::text as pvc_name
  from jsonb_array_elements(coalesce(ci.data->'spec'->'additional_disks', '[]'::jsonb))
    with ordinality as additional(disk, ordinal)
) disk
left join storage_tiers st
  on st.id = disk.disk->'storage_tier'->>'id'
where ci.tenant not in ('shared', 'system')
  and not exists (
    select 1
    from volumes v
    where v.tenant = ci.tenant
      and v.name = disk.pvc_name
  )
on conflict do nothing;

-- Network resources created as part of tenant defaults are still ordinary
-- tenant resources and count against the corresponding dimensions.
insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'external_ips', id, 'external_ip', 'external_ips', 1
from external_ips
where tenant not in ('shared', 'system')
  and (labels->>'osac.openshift.io/auto-created') is distinct from 'true'
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'nat_gateways', id, 'nat_gateway', 'nat_gateways', 1
from nat_gateways
where tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'virtual_networks', id, 'virtual_network', 'virtual_networks', 1
from virtual_networks
where tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'disk_images', id, 'disk_image', 'images', 1
from disk_images
where tenant not in ('shared', 'system')
on conflict do nothing;

-- BareMetalInstanceType is the preferred class. Template-only instances use
-- their resolved HostType as the class.
insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, class_key, units)
select bmi.tenant,
       'bare_metal_instances',
       bmi.id,
       'spec.instance_type',
         'bmaas_instances',
         case
         when bmit.id is not null then 'bmit:' || bmit.id
         else 'host_type:' || (bit.data->>'host_type')
       end,
       1
from bare_metal_instances bmi
left join bare_metal_instance_types bmit
  on bmit.id = bmi.data->'spec'->'instance_type'->>'id'
left join bare_metal_instance_templates bit
  on bit.id = bmi.data->'spec'->'template'->>'id'
where bmi.tenant not in ('shared', 'system')
  and (bmit.id is not null or nullif(bit.data->>'host_type', '') is not null)
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'bare_metal_instances', id, 'spec.auto_external_ip_attachment', 'external_ips', 1
from bare_metal_instances
where tenant not in ('shared', 'system')
  and coalesce((data->'spec'->>'auto_external_ip_attachment')::boolean, false)
on conflict do nothing;

-- A CaaS Cluster consumes a control plane plus each requested BM worker set.
-- The current CaaS path uses BareMetalInstanceTypes; legacy HostType references
-- remain classed separately until migrated by the owning service.
insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'clusters', id, 'cluster.control_plane', 'caas_control_planes', 1
from clusters
where tenant not in ('shared', 'system')
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, class_key, units)
select c.tenant,
       'clusters',
       c.id,
       'spec.node_sets.' || node_set.key,
       'bmaas_instances',
       case
         when node_set.value->'baremetal_instance_type'->>'id' is not null
           then 'bmit:' || (node_set.value->'baremetal_instance_type'->>'id')
         when node_set.value->'baremetal_instance_type'->>'name' is not null
           then 'bmit:' || (node_set.value->'baremetal_instance_type'->>'name')
         when node_set.value->'host_type'->>'id' is not null
           then 'host_type:' || (node_set.value->'host_type'->>'id')
         else 'host_type:' || (node_set.value->'host_type'->>'name')
       end,
       (node_set.value->>'size')::bigint
from clusters c
cross join lateral jsonb_each(coalesce(c.data->'spec'->'node_sets', '{}'::jsonb)) as node_set
where c.tenant not in ('shared', 'system')
  and coalesce(
    node_set.value->'baremetal_instance_type'->>'id',
    node_set.value->'baremetal_instance_type'->>'name',
    node_set.value->'host_type'->>'id',
    node_set.value->'host_type'->>'name'
  ) is not null
  and coalesce(nullif(node_set.value->>'size', '')::bigint, 0) > 0
on conflict do nothing;

insert into quota_claims (tenant, owner_type, owner_id, charge_path, dimension, units)
select tenant, 'clusters', id, 'spec.auto_external_ip_attachment', 'external_ips', 2
from clusters
where tenant not in ('shared', 'system')
  and coalesce((data->'spec'->>'auto_external_ip_attachment')::boolean, false)
on conflict do nothing;
