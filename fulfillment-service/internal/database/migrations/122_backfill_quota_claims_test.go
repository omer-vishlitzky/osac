/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package migrations

import (
	"context"

	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"
)

var _ = DescribeMigration("Backfill quota claims", func() {
	BeforeEach(func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 121)).To(Succeed())
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `
			insert into instance_types (id, name, tenant, data)
			values ('vm-small', 'vm-small', 'shared',
			  '{"spec":{"vcpus":4,"memory_gib":16,"gpu":{"resource_name":"nvidia.com/a100","count":1}}}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into bare_metal_instance_types (id, name, tenant, data)
			values ('bmit-small', 'bmit-small', 'shared', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into storage_tiers (id, name, tenant, data)
			values ('tier-gold', 'gold', 'shared', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `
			insert into compute_instances (id, name, tenant, data, deletion_timestamp)
			values ('vm-1', 'vm-one', 'tenant-a',
			  '{"spec":{"instance_type":{"id":"vm-small","name":"vm-small"},"boot_disk":{"size_gib":20,"storage_tier":{"id":"tier-gold","name":"gold"}},"additional_disks":[{"size_gib":10,"storage_tier":{"id":"tier-gold","name":"gold"}}],"auto_external_ip_attachment":true}}', now())
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into volumes (id, name, tenant, data, deletion_timestamp)
			values ('volume-root', 'vm-one-root-disk', 'tenant-a',
			  '{"spec":{"size_gib":20,"storage_tier":"gold"}}', now())
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into bare_metal_instances (id, name, tenant, data)
			values ('bm-1', 'bm-one', 'tenant-a',
			  '{"spec":{"instance_type":{"id":"bmit-small","name":"bmit-small"},"auto_external_ip_attachment":true}}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into clusters (id, name, tenant, data)
			values ('cluster-1', 'cluster-one', 'tenant-a',
			  '{"spec":{"node_sets":{"compute":{"baremetal_instance_type":{"id":"bmit-small","name":"bmit-small"},"size":2}},"auto_external_ip_attachment":true}}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into external_ips (id, name, tenant, data, labels)
			values
			  ('eip-standalone', 'public-ip', 'tenant-a', '{}', '{}'),
			  ('eip-vm-auto', 'auto-eip-vm', 'tenant-a', '{}', '{"osac.openshift.io/auto-created":"true"}')
		`)
		Expect(err).ToNot(HaveOccurred())
		var standaloneIPs int
		err = conn.QueryRow(ctx, `
			select count(*) from external_ips
			where tenant = 'tenant-a' and deletion_timestamp = 'epoch'
			  and (labels->>'osac.openshift.io/auto-created') is distinct from 'true'
		`).Scan(&standaloneIPs)
		Expect(err).ToNot(HaveOccurred())
		Expect(standaloneIPs).To(Equal(1))
		_, err = conn.Exec(ctx, `
			insert into nat_gateways (id, name, tenant, data)
			values ('nat-1', 'nat-one', 'tenant-a', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into virtual_networks (id, name, tenant, data)
			values ('vnet-1', 'vnet-one', 'tenant-a', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into disk_images (id, name, tenant, data)
			values ('image-1', 'image-one', 'tenant-a', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
	})

	It("backfills resource claims, projects usage, and transfers VM disk capacity to CSI Volume", func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 122)).To(Succeed())
		var zeroDefault int64
		err := conn.QueryRow(ctx, `
			select limit_value from tenant_quota_limits
			where tenant = 'tenant-a' and dimension = 'gpus' and class_key = ''
		`).Scan(&zeroDefault)
		Expect(err).ToNot(HaveOccurred())
		Expect(zeroDefault).To(BeZero())

		var vmTenant, vmAutoIP string
		err = conn.QueryRow(ctx, `
			select tenant, data->'spec'->>'auto_external_ip_attachment'
			from compute_instances where id = 'vm-1'
		`).Scan(&vmTenant, &vmAutoIP)
		Expect(err).ToNot(HaveOccurred())
		Expect(vmTenant).To(Equal("tenant-a"))
		Expect(vmAutoIP).To(Equal("true"))

		assertUsed := func(dimension, classKey string, expected int64) {
			var used int64
			err := conn.QueryRow(ctx, `
				select used from quota_usage
				where tenant = 'tenant-a' and dimension = $1 and class_key = $2
			`, dimension, classKey).Scan(&used)
			Expect(err).ToNot(HaveOccurred())
			Expect(used).To(Equal(expected), "%s/%s", dimension, classKey)
		}

		assertUsed("vcpus", "", 4)
		assertUsed("memory_gib", "", 16)
		assertUsed("gpus", "nvidia.com/a100", 1)
		assertUsed("volumes", "", 2)
		assertUsed("storage_gib", "gold", 30)
		assertUsed("bmaas_instances", "bmit:bmit-small", 3)
		assertUsed("caas_control_planes", "", 1)
		var standaloneClaims int
		err = conn.QueryRow(ctx, `
			select count(*) from quota_claims
			where owner_type = 'external_ips' and owner_id = 'eip-standalone'
		`).Scan(&standaloneClaims)
		Expect(err).ToNot(HaveOccurred())
		Expect(standaloneClaims).To(Equal(1))
		for _, charge := range []struct {
			ownerType string
			ownerID   string
			path      string
			units     int64
		}{
			{"external_ips", "eip-standalone", "external_ip", 1},
			{"compute_instances", "vm-1", "spec.auto_external_ip_attachment", 1},
			{"bare_metal_instances", "bm-1", "spec.auto_external_ip_attachment", 1},
			{"clusters", "cluster-1", "spec.auto_external_ip_attachment", 2},
		} {
			var units int64
			err = conn.QueryRow(ctx, `
				select units from quota_claims
				where tenant = 'tenant-a' and owner_type = $1 and owner_id = $2
				  and charge_path = $3 and dimension = 'external_ips'
			`, charge.ownerType, charge.ownerID, charge.path).Scan(&units)
			Expect(err).ToNot(HaveOccurred(), "%s/%s/%s", charge.ownerType, charge.ownerID, charge.path)
			Expect(units).To(Equal(charge.units), "%s/%s/%s", charge.ownerType, charge.ownerID, charge.path)
		}
		var externalClaimUnits int64
		err = conn.QueryRow(ctx, `
			select coalesce(sum(units), 0) from quota_claims
			where tenant = 'tenant-a' and dimension = 'external_ips'
		`).Scan(&externalClaimUnits)
		Expect(err).ToNot(HaveOccurred())
		Expect(externalClaimUnits).To(Equal(int64(5)))
		assertUsed("external_ips", "", 5)
		assertUsed("nat_gateways", "", 1)
		assertUsed("virtual_networks", "", 1)
		assertUsed("images", "", 1)

		var vmRootClaims, volumeRootClaims int
		err = conn.QueryRow(ctx, `
			select count(*) from quota_claims
			where transfer_key = 'vm-one-root-disk:storage_gib' and owner_type = 'compute_instances'
		`).Scan(&vmRootClaims)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `
			select count(*) from quota_claims
			where transfer_key = 'vm-one-root-disk:storage_gib' and owner_type = 'volumes'
		`).Scan(&volumeRootClaims)
		Expect(err).ToNot(HaveOccurred())
		Expect(vmRootClaims).To(BeZero())
		Expect(volumeRootClaims).To(Equal(1))

		var vmRootCountClaims, volumeRootCountClaims, additionalCountClaims, additionalStorageClaims int
		for _, check := range []struct {
			transferKey string
			ownerType   string
			count       *int
		}{
			{"vm-one-root-disk:volumes", "compute_instances", &vmRootCountClaims},
			{"vm-one-root-disk:volumes", "volumes", &volumeRootCountClaims},
			{"vm-one-disk-1:volumes", "compute_instances", &additionalCountClaims},
			{"vm-one-disk-1:storage_gib", "compute_instances", &additionalStorageClaims},
		} {
			err = conn.QueryRow(ctx, `
				select count(*) from quota_claims
				where tenant = 'tenant-a' and owner_type = $1 and transfer_key = $2
			`, check.ownerType, check.transferKey).Scan(check.count)
			Expect(err).ToNot(HaveOccurred())
		}
		Expect(vmRootCountClaims).To(BeZero())
		Expect(volumeRootCountClaims).To(Equal(1))
		Expect(additionalCountClaims).To(Equal(1))
		Expect(additionalStorageClaims).To(Equal(1))
		var additionalTransferKey string
		err = conn.QueryRow(ctx, `
		select transfer_key from quota_claims
			where tenant = 'tenant-a' and owner_type = 'compute_instances'
			  and owner_id = 'vm-1' and charge_path = 'spec.additional_disks[0]'
			  and dimension = 'storage_gib'
		`).Scan(&additionalTransferKey)
		Expect(err).ToNot(HaveOccurred())
		Expect(additionalTransferKey).To(Equal("vm-one-disk-1:storage_gib"))

		Expect(tool.Migrate(ctx, 121)).To(Succeed())
		Expect(tool.Migrate(ctx, 122)).To(Succeed())
		assertUsed("volumes", "", 2)
		assertUsed("storage_gib", "gold", 30)
		assertUsed("external_ips", "", 5)
	})
})
