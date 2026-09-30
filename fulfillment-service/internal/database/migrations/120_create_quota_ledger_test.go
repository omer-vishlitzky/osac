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
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"
)

var _ = DescribeMigration("Create quota ledger", func() {
	BeforeEach(func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 120)).To(Succeed())
		_, err := conn.Exec(ctx, `
			insert into quota_default_limits (dimension, class_key, limit_value)
			values ('vcpus', '', 20), ('bmaas_instances', 'bmi-small', 5)
		`)
		Expect(err).ToNot(HaveOccurred())
	})

	It("copies the configured defaults to newly created tenants", func(ctx context.Context) {
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		var limit int64
		err = conn.QueryRow(ctx, `
			select limit_value from tenant_quota_limits
			where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&limit)
		Expect(err).ToNot(HaveOccurred())
		Expect(limit).To(Equal(int64(20)))

		err = conn.QueryRow(ctx, `
			select limit_value from tenant_quota_limits
			where tenant = 'tenant-a' and dimension = 'bmaas_instances' and class_key = 'bmi-small'
		`).Scan(&limit)
		Expect(err).ToNot(HaveOccurred())
		Expect(limit).To(Equal(int64(5)))
	})

	It("projects claim inserts, updates, and deletes into aggregate usage", func(ctx context.Context) {
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		var claimID string
		err = conn.QueryRow(ctx, `
			insert into quota_claims (
				tenant, owner_type, owner_id, charge_path, dimension, class_key, units
			) values ('tenant-a', 'ComputeInstance', 'vm-1', 'spec.instance_type', 'vcpus', '', 4)
			returning claim_id
		`).Scan(&claimID)
		Expect(err).ToNot(HaveOccurred())

		var used int64
		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&used)
		Expect(err).ToNot(HaveOccurred())
		Expect(used).To(Equal(int64(4)))

		_, err = conn.Exec(ctx, `update quota_claims set units = 6 where claim_id = $1`, claimID)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&used)
		Expect(err).ToNot(HaveOccurred())
		Expect(used).To(Equal(int64(6)))

		_, err = conn.Exec(ctx, `
			update quota_claims
			set dimension = 'gpus', class_key = 'nvidia.com/a100'
			where claim_id = $1
		`, claimID)
		Expect(err).ToNot(HaveOccurred())

		var oldUsed, newUsed int64
		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'vcpus' and class_key = ''
		`).Scan(&oldUsed)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'gpus' and class_key = 'nvidia.com/a100'
		`).Scan(&newUsed)
		Expect(err).ToNot(HaveOccurred())
		Expect(oldUsed).To(Equal(int64(0)))
		Expect(newUsed).To(Equal(int64(6)))

		_, err = conn.Exec(ctx, `delete from quota_claims where claim_id = $1`, claimID)
		Expect(err).ToNot(HaveOccurred())
		err = conn.QueryRow(ctx, `
			select used from quota_usage
			where tenant = 'tenant-a' and dimension = 'gpus' and class_key = 'nvidia.com/a100'
		`).Scan(&newUsed)
		Expect(err).ToNot(HaveOccurred())
		Expect(newUsed).To(Equal(int64(0)))
	})

	It("rejects negative limits and non-positive claims", func(ctx context.Context) {
		_, err := conn.Exec(ctx, `
			insert into quota_default_limits (dimension, limit_value)
			values ('memory_gib', -1)
		`)
		Expect(err).To(HaveOccurred())
		var pgErr *pgconn.PgError
		Expect(errors.As(err, &pgErr)).To(BeTrue())
		Expect(pgErr.Code).To(Equal("23514"))

		_, err = conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
		_, err = conn.Exec(ctx, `
			insert into quota_claims (
				tenant, owner_type, owner_id, charge_path, dimension, units
			) values ('tenant-a', 'ComputeInstance', 'vm-1', 'spec.instance_type', 'vcpus', 0)
		`)
		Expect(err).To(HaveOccurred())
		Expect(errors.As(err, &pgErr)).To(BeTrue())
		Expect(pgErr.Code).To(Equal("23514"))
	})
})
