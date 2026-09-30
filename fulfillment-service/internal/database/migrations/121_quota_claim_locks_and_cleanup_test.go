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

var _ = DescribeMigration("Quota claim locks and cleanup", func() {
	It("releases claims and updates projected usage when an object is physically deleted", func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 121)).To(Succeed())
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `
			insert into quota_claims (
				tenant, owner_type, owner_id, charge_path, dimension, class_key, units
			) values ('tenant-a', 'compute_instances', 'vm-1', 'spec.instance_type', 'vcpus', '', 4)
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `
			insert into compute_instances (id, name, tenant, data)
			values ('vm-1', 'vm-1', 'tenant-a', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `delete from compute_instances where id = 'vm-1'`)
		Expect(err).ToNot(HaveOccurred())

		var claims, used int64
		err = conn.QueryRow(ctx, `
			select count(*) from quota_claims
			where tenant = 'tenant-a' and owner_type = 'compute_instances' and owner_id = 'vm-1'
		`).Scan(&claims)
		Expect(err).ToNot(HaveOccurred())
		Expect(claims).To(BeZero())

		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&used)
		Expect(err).ToNot(HaveOccurred())
		Expect(used).To(BeZero())
	})

	It("keeps claims while an object is soft-deleted and releases them after cleanup deletion", func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 121)).To(Succeed())
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into compute_instances (id, name, tenant, data)
			values ('vm-deleting', 'vm-deleting', 'tenant-a', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into quota_claims (
				tenant, owner_type, owner_id, charge_path, dimension, class_key, units
			) values ('tenant-a', 'compute_instances', 'vm-deleting', 'spec.instance_type', 'vcpus', '', 4)
		`)
		Expect(err).ToNot(HaveOccurred())

		_, err = conn.Exec(ctx, `
			update compute_instances set deletion_timestamp = now() where id = 'vm-deleting'
		`)
		Expect(err).ToNot(HaveOccurred())
		var used int64
		err = conn.QueryRow(ctx, `
			select used from quota_usage where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&used)
		Expect(err).ToNot(HaveOccurred())
		Expect(used).To(Equal(int64(4)))

		_, err = conn.Exec(ctx, `delete from compute_instances where id = 'vm-deleting'`)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `
			select used from quota_usage where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&used)
		Expect(err).ToNot(HaveOccurred())
		Expect(used).To(BeZero())
	})

	It("uses distinct advisory lock keys for tenant, dimension, and class", func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 121)).To(Succeed())

		var same, otherTenant, otherDimension, otherClass int64
		err := conn.QueryRow(ctx, `select quota_lock_key('usage', 'tenant-a', 'vcpus', '')`).Scan(&same)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `select quota_lock_key('usage', 'tenant-b', 'vcpus', '')`).Scan(&otherTenant)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `select quota_lock_key('usage', 'tenant-a', 'memory_gib', '')`).Scan(&otherDimension)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `select quota_lock_key('usage', 'tenant-a', 'gpus', 'nvidia.com/a100')`).Scan(&otherClass)
		Expect(err).ToNot(HaveOccurred())

		Expect(otherTenant).NotTo(Equal(same))
		Expect(otherDimension).NotTo(Equal(same))
		Expect(otherClass).NotTo(Equal(same))
	})
})
