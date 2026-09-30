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

package quota

import (
	"context"
	"errors"
	"sync"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/database"
)

var _ = Describe("Quota charges", func() {
	It("combines duplicate claims and sorts them deterministically", func() {
		charges, err := NormalizeCharges([]Charge{
			{Dimension: "memory_gib", Path: "spec.instance_type", Units: 16},
			{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
			{Dimension: "memory_gib", Path: "spec.instance_type", Units: 8},
		})
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(Equal([]Charge{
			{Dimension: "memory_gib", Path: "spec.instance_type", Units: 24},
			{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
		}))
	})

	It("rejects invalid charges and ignores zero-unit charges", func() {
		charges, err := NormalizeCharges([]Charge{
			{Dimension: "vcpus", Path: "spec.instance_type", Units: 0},
		})
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(BeEmpty())

		_, err = NormalizeCharges([]Charge{{Path: "spec.instance_type", Units: 1}})
		Expect(err).To(MatchError("quota charge dimension is required"))

		_, err = NormalizeCharges([]Charge{{Dimension: "vcpus", Units: 1}})
		Expect(err).To(MatchError("quota charge path is required"))

		_, err = NormalizeCharges([]Charge{{Dimension: "vcpus", Path: "spec.instance_type", Units: -1}})
		Expect(err).To(MatchError("quota charge units cannot be negative for spec.instance_type"))
	})

	It("rejects charge totals that overflow int64", func() {
		_, err := NormalizeCharges([]Charge{
			{Dimension: "vcpus", Path: "first", Units: int64(^uint64(0) >> 1)},
			{Dimension: "vcpus", Path: "first", Units: 1},
		})
		Expect(err).To(MatchError("quota charge units overflow for first"))
	})

	It("returns ResourceExhausted with a structured quota violation", func() {
		err := (&ExceededError{
			Tenant:    "tenant-a",
			Dimension: "bmaas_instances",
			ClassKey:  "bmi-small",
			Path:      "spec.node_sets.workers",
			Used:      5,
			Requested: 1,
			Limit:     5,
		}).GRPCStatus().Err()

		Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
		Expect(err.Error()).To(ContainSubstring("current usage 5, request 1, limit 5"))
	})
})

var _ = Describe("Quota store", func() {
	const tenantName = "quota-test-tenant"

	var (
		ctx       context.Context
		db        *database.Instance
		pool      *pgxpool.Pool
		txManager database.TxManager
		store     *Store
	)

	BeforeEach(func() {
		ctx = context.Background()
		var err error
		db, err = quotaDatabase.NewInstance().Build()
		Expect(err).ToNot(HaveOccurred())
		DeferCleanup(db.Close)

		pool, err = db.Pool(ctx)
		Expect(err).ToNot(HaveOccurred())
		DeferCleanup(pool.Close)

		txManager, err = database.NewTxManager().SetLogger(quotaLogger).SetPool(pool).Build()
		Expect(err).ToNot(HaveOccurred())
		store = NewStore()

		_, err = pool.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ($1, $1, $1, 'system', '{}')
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())
		_, err = pool.Exec(ctx, `
			insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
			values ($1, 'vcpus', '', 5), ($1, 'memory_gib', '', 100),
			       ($1, 'bmaas_instances', 'bmi-small', 5),
			       ($1, 'volumes', '', 5), ($1, 'storage_gib', 'gold', 1000)
			on conflict (tenant, dimension, class_key) do update
			set limit_value = excluded.limit_value
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())
	})

	runTransaction := func(task func(context.Context) error) error {
		return txManager.Run(ctx, task)
	}

	readUsed := func(dimension, classKey string) int64 {
		var used int64
		err := pool.QueryRow(ctx, `
			select used from quota_usage where tenant = $1 and dimension = $2 and class_key = $3
		`, tenantName, dimension, classKey).Scan(&used)
		if errors.Is(err, pgx.ErrNoRows) {
			return 0
		}
		Expect(err).ToNot(HaveOccurred())
		return used
	}

	owner := func(id string) Owner {
		return Owner{Tenant: tenantName, Type: "compute_instances", ID: id}
	}

	It("records accepted claims and denies claims that exceed a limit", func() {
		vm1 := owner("vm-1")
		err := runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm1, []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
				{Dimension: "memory_gib", Path: "spec.instance_type", Units: 16},
			}, false)
		})
		Expect(err).ToNot(HaveOccurred())
		Expect(readUsed("vcpus", "")).To(Equal(int64(4)))
		Expect(readUsed("memory_gib", "")).To(Equal(int64(16)))

		vm2 := owner("vm-2")
		err = runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm2, []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 2},
				{Dimension: "memory_gib", Path: "spec.instance_type", Units: 8},
			}, false)
		})
		Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
		Expect(err.Error()).To(ContainSubstring("current usage 4, request 2, limit 5"))
		Expect(readUsed("vcpus", "")).To(Equal(int64(4)))
		Expect(readUsed("memory_gib", "")).To(Equal(int64(16)))

		var count int
		err = pool.QueryRow(ctx, `select count(*) from quota_claims where owner_id = 'vm-2'`).Scan(&count)
		Expect(err).ToNot(HaveOccurred())
		Expect(count).To(BeZero())
	})

	It("allows a claim reduction when the tenant is already over its lowered limit", func() {
		vm := owner("vm-1")
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
			}, false)
		})).To(Succeed())

		_, err := pool.Exec(ctx, `
			update tenant_quota_limits set limit_value = 3
			where tenant = $1 and dimension = 'vcpus' and class_key = ''
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())

		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 2},
			}, false)
		})).To(Succeed())
		Expect(readUsed("vcpus", "")).To(Equal(int64(2)))
	})

	It("uses the dimension default for an unset class and prefers an explicit class limit", func() {
		_, err := pool.Exec(ctx, `
			update tenant_quota_limits set limit_value = 3
			where tenant = $1 and dimension = 'gpus' and class_key = ''
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())

		gpuOwner := Owner{Tenant: tenantName, Type: "compute_instances", ID: "gpu-vm"}
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, gpuOwner, []Charge{
				{Dimension: "gpus", ClassKey: "nvidia.com/a100", Path: "spec.instance_type.gpu", Units: 2},
			}, false)
		})).To(Succeed())

		_, err = pool.Exec(ctx, `
			insert into tenant_quota_limits (tenant, dimension, class_key, limit_value)
			values ($1, 'gpus', 'nvidia.com/a100', 1)
			on conflict (tenant, dimension, class_key) do update
			set limit_value = excluded.limit_value
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())
		otherOwner := Owner{Tenant: tenantName, Type: "compute_instances", ID: "gpu-vm-2"}
		err = runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, otherOwner, []Charge{
				{Dimension: "gpus", ClassKey: "nvidia.com/a100", Path: "spec.instance_type.gpu", Units: 1},
			}, false)
		})
		Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
		Expect(readUsed("gpus", "nvidia.com/a100")).To(Equal(int64(2)))
	})

	It("updates limits and warning headroom, reports over-limit usage, and audits the change", func() {
		Expect(runTransaction(func(ctx context.Context) error {
			return store.SetTenantLimit(ctx, tenantName, "gpus", "", 3, "provider-admin")
		})).To(Succeed())
		vm := owner("vm-usage-view")
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
				{Dimension: "gpus", ClassKey: "nvidia.com/a100", Path: "spec.instance_type.gpu", Units: 2},
			}, false)
		})).To(Succeed())

		Expect(runTransaction(func(ctx context.Context) error {
			return store.SetTenantLimit(ctx, tenantName, "vcpus", "", 3, "provider-admin")
		})).To(Succeed())
		threshold := int64(1)
		Expect(runTransaction(func(ctx context.Context) error {
			return store.SetWarningThreshold(ctx, tenantName, "vcpus", &threshold)
		})).To(Succeed())

		var cpuUsage, gpuUsage *Usage
		var usage []Usage
		Expect(runTransaction(func(ctx context.Context) error {
			var err error
			usage, err = store.GetUsage(ctx, tenantName)
			return err
		})).To(Succeed())
		for index := range usage {
			item := &usage[index]
			if item.Dimension == "vcpus" && item.ClassKey == "" {
				cpuUsage = item
			}
			if item.Dimension == "gpus" && item.ClassKey == "nvidia.com/a100" {
				gpuUsage = item
			}
		}
		Expect(cpuUsage).ToNot(BeNil())
		Expect(cpuUsage.Used).To(Equal(int64(4)))
		Expect(cpuUsage.Limit).To(Equal(int64(3)))
		Expect(cpuUsage.Remaining).To(Equal(int64(-1)))
		Expect(cpuUsage.OverLimit).To(BeTrue())
		Expect(cpuUsage.WarningThreshold).ToNot(BeNil())
		Expect(*cpuUsage.WarningThreshold).To(Equal(int64(1)))
		Expect(gpuUsage).ToNot(BeNil())
		Expect(gpuUsage.Limit).To(Equal(int64(3)))

		var previous, next int64
		var actor string
		err := pool.QueryRow(ctx, `
			select previous_limit, new_limit, actor from quota_limit_audit
			where tenant = $1 and dimension = 'vcpus' and class_key = ''
		`, tenantName).Scan(&previous, &next, &actor)
		Expect(err).ToNot(HaveOccurred())
		Expect(previous).To(Equal(int64(5)))
		Expect(next).To(Equal(int64(3)))
		Expect(actor).To(Equal("provider-admin"))

		Expect(runTransaction(func(ctx context.Context) error {
			return store.SetWarningThreshold(ctx, tenantName, "vcpus", nil)
		})).To(Succeed())
		Expect(runTransaction(func(ctx context.Context) error {
			var err error
			usage, err = store.GetUsage(ctx, tenantName)
			return err
		})).To(Succeed())
		for _, item := range usage {
			if item.Dimension == "vcpus" && item.ClassKey == "" {
				Expect(item.WarningThreshold).To(BeNil())
			}
		}
	})

	It("copies updated defaults only to tenants created afterward", func() {
		Expect(runTransaction(func(ctx context.Context) error {
			return store.SetDefaultLimit(ctx, "vcpus", "", 20, "provider-admin")
		})).To(Succeed())

		_, err := pool.Exec(ctx, `
			insert into tenants (id, name, tenant, creator, data)
			values ('quota-test-new-tenant', 'quota-test-new-tenant', 'quota-test-new-tenant', 'system', '{}')
		`)
		Expect(err).ToNot(HaveOccurred())

		var existingLimit, newLimit int64
		err = pool.QueryRow(ctx, `
			select limit_value from tenant_quota_limits
			where tenant = $1 and dimension = 'vcpus' and class_key = ''
		`, tenantName).Scan(&existingLimit)
		Expect(err).ToNot(HaveOccurred())
		err = pool.QueryRow(ctx, `
			select limit_value from tenant_quota_limits
			where tenant = 'quota-test-new-tenant' and dimension = 'vcpus' and class_key = ''
		`).Scan(&newLimit)
		Expect(err).ToNot(HaveOccurred())
		Expect(existingLimit).To(Equal(int64(5)))
		Expect(newLimit).To(Equal(int64(20)))

		var auditCount int
		err = pool.QueryRow(ctx, `
			select count(*) from quota_limit_audit
			where tenant is null and dimension = 'vcpus' and class_key = '' and actor = 'provider-admin'
		`).Scan(&auditCount)
		Expect(err).ToNot(HaveOccurred())
		Expect(auditCount).To(Equal(1))
	})

	It("does not partially record a multi-dimension claim when one dimension is over quota", func() {
		vm := owner("vm-1")
		err := runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, []Charge{
				{Dimension: "memory_gib", Path: "spec.instance_type", Units: 101},
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 1},
			}, false)
		})
		Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
		Expect(readUsed("vcpus", "")).To(BeZero())
		Expect(readUsed("memory_gib", "")).To(BeZero())
	})

	It("rejects VM admission when a backing disk would exceed the volume-count limit", func() {
		_, err := pool.Exec(ctx, `
			update tenant_quota_limits set limit_value = 0
			where tenant = $1 and dimension = 'volumes' and class_key = ''
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())

		err = runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, owner("vm-no-volume-quota"), []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 2},
				{Dimension: "volumes", Path: "spec.boot_disk", Units: 1, TransferKey: "vm-no-volume-quota-root-disk:volumes"},
				{Dimension: "storage_gib", ClassKey: "gold", Path: "spec.boot_disk", Units: 20, TransferKey: "vm-no-volume-quota-root-disk:storage_gib"},
			}, false)
		})
		Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
		Expect(err.Error()).To(ContainSubstring("quota exceeded for volumes"))
		Expect(readUsed("vcpus", "")).To(BeZero())
		Expect(readUsed("volumes", "")).To(BeZero())
		Expect(readUsed("storage_gib", "gold")).To(BeZero())
	})

	It("serializes concurrent admissions on the same quota key", func() {
		_, err := pool.Exec(ctx, `
			update tenant_quota_limits set limit_value = 1
			where tenant = $1 and dimension = 'vcpus' and class_key = ''
		`, tenantName)
		Expect(err).ToNot(HaveOccurred())

		var wait sync.WaitGroup
		results := make(chan error, 2)
		for _, id := range []string{"vm-1", "vm-2"} {
			wait.Add(1)
			go func(id string) {
				defer wait.Done()
				results <- runTransaction(func(ctx context.Context) error {
					return store.AdmitAndReplace(ctx, owner(id), []Charge{
						{Dimension: "vcpus", Path: "spec.instance_type", Units: 1},
					}, false)
				})
			}(id)
		}
		wait.Wait()
		close(results)

		var passed, rejected int
		for err := range results {
			if err == nil {
				passed++
				continue
			}
			Expect(status.Code(err)).To(Equal(codes.ResourceExhausted))
			rejected++
		}
		Expect(passed).To(Equal(1))
		Expect(rejected).To(Equal(1))
		Expect(readUsed("vcpus", "")).To(Equal(int64(1)))
	})

	It("checks dry-run charges without persisting claims or usage rows", func() {
		err := runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, owner("vm-dry-run"), []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 1},
			}, true)
		})
		Expect(err).ToNot(HaveOccurred())
		Expect(readUsed("vcpus", "")).To(BeZero())

		var count int
		err = pool.QueryRow(ctx, `select count(*) from quota_claims where owner_id = 'vm-dry-run'`).Scan(&count)
		Expect(err).ToNot(HaveOccurred())
		Expect(count).To(BeZero())
	})

	It("transfers VM disk storage claims to CSI Volumes without double counting", func() {
		vm := owner("vm-1")
		vmCharges := []Charge{
			{Dimension: "vcpus", Path: "spec.instance_type", Units: 4},
			{Dimension: "volumes", Path: "spec.boot_disk", Units: 1, TransferKey: "vm-1-root-disk:volumes"},
			{Dimension: "storage_gib", ClassKey: "gold", Path: "spec.boot_disk", Units: 50, TransferKey: "vm-1-root-disk:storage_gib"},
		}
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, vmCharges, false)
		})).To(Succeed())
		Expect(readUsed("storage_gib", "gold")).To(Equal(int64(50)))
		Expect(readUsed("volumes", "")).To(Equal(int64(1)))

		volume := Owner{Tenant: tenantName, Type: "volumes", ID: "volume-1"}
		volumeCharges := []Charge{
			{Dimension: "volumes", Path: "object", Units: 1, TransferKey: "vm-1-root-disk:volumes"},
			{Dimension: "storage_gib", ClassKey: "gold", Path: "spec.size_gib", Units: 50, TransferKey: "vm-1-root-disk:storage_gib"},
		}
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, volume, volumeCharges, false)
		})).To(Succeed())
		Expect(readUsed("storage_gib", "gold")).To(Equal(int64(50)))
		Expect(readUsed("volumes", "")).To(Equal(int64(1)))

		// A VM status update still includes its declared disk in the plan. The
		// existing Volume claim owns that storage now, so the VM update must not
		// recreate a second charge.
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, vm, vmCharges, false)
		})).To(Succeed())
		Expect(readUsed("storage_gib", "gold")).To(Equal(int64(50)))

		Expect(runTransaction(func(ctx context.Context) error {
			return store.ReleaseOwner(ctx, vm)
		})).To(Succeed())
		Expect(readUsed("storage_gib", "gold")).To(Equal(int64(50)))
		Expect(readUsed("volumes", "")).To(Equal(int64(1)))

		Expect(runTransaction(func(ctx context.Context) error {
			return store.ReleaseOwner(ctx, volume)
		})).To(Succeed())
		Expect(readUsed("storage_gib", "gold")).To(BeZero())
		Expect(readUsed("volumes", "")).To(BeZero())
	})

	It("rejects a VM disk reservation when another Volume already owns its transfer key", func() {
		volume := Owner{Tenant: tenantName, Type: "volumes", ID: "volume-1"}
		Expect(runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, volume, []Charge{
				{Dimension: "volumes", Path: "object", Units: 1, TransferKey: "vm-1-root-disk:volumes"},
				{Dimension: "storage_gib", ClassKey: "gold", Path: "spec.size_gib", Units: 50, TransferKey: "vm-1-root-disk:storage_gib"},
			}, false)
		})).To(Succeed())

		err := runTransaction(func(ctx context.Context) error {
			return store.AdmitAndReplace(ctx, owner("vm-2"), []Charge{
				{Dimension: "vcpus", Path: "spec.instance_type", Units: 1},
				{Dimension: "volumes", Path: "spec.boot_disk", Units: 1, TransferKey: "vm-1-root-disk:volumes"},
				{Dimension: "storage_gib", ClassKey: "gold", Path: "spec.boot_disk", Units: 50, TransferKey: "vm-1-root-disk:storage_gib"},
			}, false)
		})
		Expect(status.Code(err)).To(Equal(codes.FailedPrecondition))
		Expect(readUsed("storage_gib", "gold")).To(Equal(int64(50)))
		Expect(readUsed("vcpus", "")).To(BeZero())
	})
})
