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
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/database"
)

// Usage is the current tenant usage for a dimension and optional class.
type Usage struct {
	Dimension        string
	ClassKey         string
	Used             int64
	Limit            int64
	Remaining        int64
	OverLimit        bool
	WarningThreshold *int64
}

// Limit is a configured default limit.
type Limit struct {
	Dimension string
	ClassKey  string
	Value     int64
}

// AuditRecord is a quota limit change.
type AuditRecord struct {
	Tenant        string
	Dimension     string
	ClassKey      string
	PreviousLimit *int64
	NewLimit      int64
	Actor         string
	ChangedAt     time.Time
}

// IsSupportedDimension reports whether the quota engine can calculate a dimension.
func IsSupportedDimension(dimension string) bool {
	switch dimension {
	case DimensionVCPUs,
		DimensionMemoryGiB,
		DimensionGPUs,
		DimensionVolumeCount,
		DimensionStorageGiB,
		DimensionDiskImages,
		DimensionExternalIPs,
		DimensionNATGateways,
		DimensionVirtualNetworks,
		DimensionBareMetalInstances,
		DimensionCaaSControlPlanes:
		return true
	default:
		return false
	}
}

func ValidateDimensionClass(dimension, classKey string) error {
	if !IsSupportedDimension(dimension) {
		return fmt.Errorf("unsupported quota dimension %q", dimension)
	}
	if classKey == "" {
		return nil
	}
	switch dimension {
	case DimensionGPUs, DimensionStorageGiB, DimensionBareMetalInstances:
		return nil
	default:
		return fmt.Errorf("quota dimension %q does not have resource classes", dimension)
	}
}

// GetUsage returns the configured and used quota keys for a tenant. A class
// without an explicit tenant limit uses the dimension-wide tenant limit.
func (s *Store) GetUsage(ctx context.Context, tenant string) ([]Usage, error) {
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, `
		with quota_keys as (
			select dimension, class_key from tenant_quota_limits where tenant = $1
			union
			select dimension, class_key from quota_usage where tenant = $1
		)
		select keys.dimension,
		       keys.class_key,
		       coalesce(usage.used, 0),
	       coalesce(class_limit.limit_value, dimension_limit.limit_value, 0),
	       coalesce(class_limit.warning_threshold, dimension_limit.warning_threshold)
		from quota_keys keys
		left join quota_usage usage
		  on usage.tenant = $1 and usage.dimension = keys.dimension and usage.class_key = keys.class_key
		left join tenant_quota_limits class_limit
		  on class_limit.tenant = $1 and class_limit.dimension = keys.dimension and class_limit.class_key = keys.class_key
		left join tenant_quota_limits dimension_limit
		  on dimension_limit.tenant = $1 and dimension_limit.dimension = keys.dimension and dimension_limit.class_key = ''
		order by keys.dimension, keys.class_key
	`, tenant)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	result := make([]Usage, 0)
	for rows.Next() {
		var item Usage
		var warningThreshold pgtype.Int8
		if err = rows.Scan(&item.Dimension, &item.ClassKey, &item.Used, &item.Limit, &warningThreshold); err != nil {
			return nil, err
		}
		item.Remaining = item.Limit - item.Used
		item.OverLimit = item.Used > item.Limit
		if warningThreshold.Valid {
			item.WarningThreshold = &warningThreshold.Int64
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

// SetDefaultLimit changes the default copied to newly created tenants.
func (s *Store) SetDefaultLimit(ctx context.Context, dimension, classKey string, value int64, actor string) (err error) {
	if err = validateLimit(dimension, classKey, value, actor); err != nil {
		return
	}
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return
	}
	defer tx.ReportError(&err)

	if err = lock(ctx, tx, "defaults", "", "", ""); err != nil {
		return
	}
	previous := pgtype.Int8{}
	err = tx.QueryRow(ctx, `
		select limit_value from quota_default_limits
		where dimension = $1 and class_key = $2
		for update
	`, dimension, classKey).Scan(&previous)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return
	}
	if _, err = tx.Exec(ctx, `
		insert into quota_default_limits (dimension, class_key, limit_value, updated_at)
		values ($1, $2, $3, now())
		on conflict (dimension, class_key) do update
		set limit_value = excluded.limit_value, updated_at = now()
	`, dimension, classKey, value); err != nil {
		return
	}
	_, err = tx.Exec(ctx, `
		insert into quota_limit_audit (tenant, dimension, class_key, previous_limit, new_limit, actor)
		values (null, $1, $2, $3, $4, $5)
	`, dimension, classKey, nullableInt8(previous), value, actor)
	return
}

// SetTenantLimit changes a tenant limit. Lowering the limit below current usage
// is allowed; subsequent admissions observe the new limit under the same key lock.
func (s *Store) SetTenantLimit(ctx context.Context, tenant, dimension, classKey string, value int64, actor string) (err error) {
	if tenant == "" {
		return grpcstatus.Error(grpccodes.InvalidArgument, "quota tenant is required")
	}
	if err = validateLimit(dimension, classKey, value, actor); err != nil {
		return
	}
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return
	}
	defer tx.ReportError(&err)

	if err = lock(ctx, tx, "usage", tenant, dimension, classKey); err != nil {
		return
	}
	previous, readErr := readLimit(ctx, tx, tenant, usageKey{dimension: dimension, classKey: classKey})
	if readErr != nil {
		err = readErr
		return
	}
	if _, err = tx.Exec(ctx, `
		insert into tenant_quota_limits (tenant, dimension, class_key, limit_value, updated_at)
		values ($1, $2, $3, $4, now())
		on conflict (tenant, dimension, class_key) do update
		set limit_value = excluded.limit_value, updated_at = now()
	`, tenant, dimension, classKey, value); err != nil {
		return
	}
	_, err = tx.Exec(ctx, `
		insert into quota_limit_audit (tenant, dimension, class_key, previous_limit, new_limit, actor)
		values ($1, $2, $3, $4, $5, $6)
	`, tenant, dimension, classKey, previous, value, actor)
	return
}

// SetWarningThreshold sets or clears a tenant's dimension-wide headroom threshold.
func (s *Store) SetWarningThreshold(ctx context.Context, tenant, dimension string, threshold *int64) (err error) {
	if tenant == "" {
		return grpcstatus.Error(grpccodes.InvalidArgument, "quota tenant is required")
	}
	if !IsSupportedDimension(dimension) {
		return grpcstatus.Errorf(grpccodes.InvalidArgument, "unsupported quota dimension %q", dimension)
	}
	if threshold != nil && *threshold < 0 {
		return grpcstatus.Error(grpccodes.InvalidArgument, "quota warning threshold cannot be negative")
	}
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return
	}
	defer tx.ReportError(&err)

	var commandTag pgconn.CommandTag
	commandTag, err = tx.Exec(ctx, `
		update tenant_quota_limits
		set warning_threshold = $3, updated_at = now()
		where tenant = $1 and dimension = $2 and class_key = ''
	`, tenant, dimension, threshold)
	if err != nil {
		return
	}
	if commandTag.RowsAffected() == 0 {
		return grpcstatus.Errorf(grpccodes.NotFound, "quota limit for tenant %q and dimension %q was not found", tenant, dimension)
	}
	return
}

// GetDefaultLimits returns configured dimension-wide and class-specific defaults.
func (s *Store) GetDefaultLimits(ctx context.Context) (result []Limit, err error) {
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, `
		select dimension, class_key, limit_value
		from quota_default_limits
		order by dimension, class_key
	`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var item Limit
		if err = rows.Scan(&item.Dimension, &item.ClassKey, &item.Value); err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

// ListAuditRecords returns limit changes, optionally filtered by tenant.
func (s *Store) ListAuditRecords(
	ctx context.Context,
	tenant string,
	limit int32,
	offset int32,
) (result []AuditRecord, total int32, err error) {
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return nil, 0, err
	}
	err = tx.QueryRow(ctx, `
		select count(*) from quota_limit_audit where ($1 = '' or tenant = $1)
	`, tenant).Scan(&total)
	if err != nil {
		return nil, 0, err
	}
	rows, err := tx.Query(ctx, `
		select coalesce(tenant, ''), dimension, class_key, previous_limit, new_limit, actor, changed_at
		from quota_limit_audit
		where ($1 = '' or tenant = $1)
		order by changed_at desc, id
		limit $2 offset $3
	`, tenant, limit, offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		var item AuditRecord
		var previous pgtype.Int8
		if err = rows.Scan(&item.Tenant, &item.Dimension, &item.ClassKey, &previous,
			&item.NewLimit, &item.Actor, &item.ChangedAt); err != nil {
			return nil, 0, err
		}
		if previous.Valid {
			item.PreviousLimit = &previous.Int64
		}
		result = append(result, item)
	}
	return result, total, rows.Err()
}

func validateLimit(dimension, classKey string, value int64, actor string) error {
	if err := ValidateDimensionClass(dimension, classKey); err != nil {
		return grpcstatus.Error(grpccodes.InvalidArgument, err.Error())
	}
	if value < 0 {
		return grpcstatus.Error(grpccodes.InvalidArgument, "quota limit cannot be negative")
	}
	if actor == "" {
		return grpcstatus.Error(grpccodes.InvalidArgument, "quota change actor is required")
	}
	return nil
}

func nullableInt8(value pgtype.Int8) any {
	if !value.Valid {
		return nil
	}
	return value.Int64
}
