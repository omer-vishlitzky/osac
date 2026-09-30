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

var _ = DescribeMigration("Create quota increase requests", func() {
	BeforeEach(func(ctx context.Context) {
		Expect(tool.Migrate(ctx, 123)).To(Succeed())
		_, err := conn.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('tenant-a', 'tenant-a', 'tenant-a', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())
	})

	It("stores pending requests and rejects an invalid status", func(ctx context.Context) {
		_, err := conn.Exec(ctx, `
			insert into quota_increase_requests (
				id, tenant, name, creator, dimension, class_key, requested_limit, reason
			) values ('req-1', 'tenant-a', 'more-gpus', 'user-a', 'gpus', 'nvidia.com/a100', 4, 'Need a GPU for training')
		`)
		Expect(err).ToNot(HaveOccurred())

		var state string
		err = conn.QueryRow(ctx, `
			select state from quota_increase_requests where id = 'req-1'
		`).Scan(&state)
		Expect(err).ToNot(HaveOccurred())
		Expect(state).To(Equal("PENDING"))

		_, err = conn.Exec(ctx, `
			update quota_increase_requests set state = 'UNKNOWN' where id = 'req-1'
		`)
		Expect(err).To(HaveOccurred())
		var pgErr *pgconn.PgError
		Expect(errors.As(err, &pgErr)).To(BeTrue())
		Expect(pgErr.Code).To(Equal("23514"))
	})
})
