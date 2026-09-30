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
	"math"
	"sort"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/database"
)

// Store reads tenant limits and replaces the claims owned by a resource. All
// operations require the transaction installed by TxInterceptor.
type Store struct{}

func NewStore() *Store {
	return &Store{}
}

type usageKey struct {
	dimension string
	classKey  string
}

type claimRecord struct {
	ownerType string
	ownerID   string
	charge    Charge
}

// AdmitAndReplace serializes competing admissions for every affected quota
// key, checks the new aggregate, then replaces the owner's claims. The claim
// trigger updates quota_usage before this RPC transaction commits.
func (s *Store) AdmitAndReplace(ctx context.Context, owner Owner, charges []Charge, dryRun bool) (err error) {
	if owner.Tenant == "" {
		return errors.New("quota owner tenant is required")
	}
	if owner.Type == "" {
		return errors.New("quota owner type is required")
	}
	if !dryRun && owner.ID == "" {
		return errors.New("quota owner id is required")
	}

	charges, err = NormalizeCharges(charges)
	if err != nil {
		return
	}
	if charges == nil && dryRun {
		charges = []Charge{}
	}

	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return
	}
	defer tx.ReportError(&err)

	if owner.ID != "" {
		if err = lock(ctx, tx, "owner", owner.Tenant, owner.Type, owner.ID); err != nil {
			return
		}
	}
	oldChargeHints, err := readOwnerCharges(ctx, tx, owner, false)
	if err != nil {
		return
	}
	for _, transferKey := range uniqueTransferKeys(oldChargeHints, charges) {
		if err = lock(ctx, tx, "transfer", owner.Tenant, transferKey, ""); err != nil {
			return
		}
	}
	oldCharges, err := readOwnerCharges(ctx, tx, owner, true)
	if err != nil {
		return
	}

	effectiveCharges, transfers, noOp, err := resolveTransfers(ctx, tx, owner, oldCharges, charges)
	if err != nil {
		return
	}
	if noOp {
		return nil
	}

	oldForDelta := append([]Charge(nil), oldCharges...)
	for _, transfer := range transfers {
		oldForDelta = append(oldForDelta, transfer.charge)
	}
	keys := quotaKeys(oldForDelta, effectiveCharges)
	for _, key := range keys {
		if err = lock(ctx, tx, "usage", owner.Tenant, key.dimension, key.classKey); err != nil {
			return
		}
	}

	if err = checkQuotaLimits(ctx, tx, owner.Tenant, keys, oldForDelta, effectiveCharges, charges); err != nil {
		return
	}

	if dryRun {
		return nil
	}
	return replaceOwnerClaims(ctx, tx, owner, effectiveCharges, transfers)
}

func resolveTransfers(
	ctx context.Context,
	tx database.Tx,
	owner Owner,
	oldCharges, charges []Charge,
) (effective []Charge, transfers []claimRecord, noOp bool, err error) {
	effective = make([]Charge, 0, len(charges))
	transfers = make([]claimRecord, 0, len(charges))
	for _, charge := range charges {
		if charge.TransferKey == "" {
			effective = append(effective, charge)
			continue
		}
		claim, found, lookupErr := readTransferClaim(ctx, tx, owner.Tenant, charge.TransferKey)
		if lookupErr != nil {
			return nil, nil, false, lookupErr
		}
		if !found || (claim.ownerType == owner.Type && claim.ownerID == owner.ID) {
			effective = append(effective, charge)
			continue
		}

		switch {
		case owner.Type == "volumes" && claim.ownerType == "compute_instances":
			if !sameChargeUnits(claim.charge, charge) {
				return nil, nil, false, status.Errorf(codes.FailedPrecondition,
					"quota reservation for volume charge %q does not match dimension %q",
					charge.Path, charge.Dimension)
			}
			transfers = append(transfers, claimRecord{
				ownerType: claim.ownerType,
				ownerID:   claim.ownerID,
				charge:    charge,
			})
			effective = append(effective, charge)
		case owner.Type == "volumes" && claim.ownerType == "volumes":
			// A retried CSI CreateVolume will reach the DAO, which returns
			// AlreadyExists. Do not charge the same logical PVC a second time.
			return nil, nil, true, nil
		case owner.Type == "compute_instances" && claim.ownerType == "volumes":
			if len(oldCharges) == 0 {
				return nil, nil, false, status.Errorf(codes.FailedPrecondition,
					"a volume already exists for requested VM disk charge %q", charge.Path)
			}
			if !sameChargeUnits(claim.charge, charge) {
				return nil, nil, false, status.Errorf(codes.FailedPrecondition,
					"VM disk charge %q no longer matches its backing volume dimension %q",
					charge.Path, charge.Dimension)
			}
			// The reservation was transferred to its backing Volume. Keep the
			// Volume's claim and do not add the same disk charge back to the VM.
		case owner.Type == "compute_instances" && claim.ownerType == "compute_instances":
			return nil, nil, false, status.Errorf(codes.AlreadyExists,
				"a quota reservation already exists for VM disk charge %q", charge.Path)
		default:
			return nil, nil, false, status.Errorf(codes.FailedPrecondition,
				"quota charge %q is already owned by %s %q",
				charge.Path, claim.ownerType, claim.ownerID)
		}
	}
	return effective, transfers, false, nil
}

func checkQuotaLimits(
	ctx context.Context,
	tx database.Tx,
	tenant string,
	keys []usageKey,
	oldCharges, newCharges, requestedCharges []Charge,
) error {
	oldUnits, err := sumByQuotaKey(oldCharges)
	if err != nil {
		return err
	}
	newUnits, err := sumByQuotaKey(newCharges)
	if err != nil {
		return err
	}
	for _, key := range keys {
		used, err := readUsage(ctx, tx, tenant, key)
		if err != nil {
			return err
		}
		limit, err := readLimit(ctx, tx, tenant, key)
		if err != nil {
			return err
		}
		delta := newUnits[key] - oldUnits[key]
		if delta <= 0 {
			continue
		}
		if used > math.MaxInt64-delta {
			return fmt.Errorf("quota usage overflow for %s", key.dimension)
		}
		if used+delta > limit {
			charge := chargeForKey(requestedCharges, key)
			return &ExceededError{
				Tenant:    tenant,
				Dimension: key.dimension,
				ClassKey:  key.classKey,
				Path:      charge.Path,
				Used:      used,
				Requested: delta,
				Limit:     limit,
			}
		}
	}
	return nil
}

func (s *Store) ReleaseOwner(ctx context.Context, owner Owner) (err error) {
	return s.AdmitAndReplace(ctx, owner, nil, false)
}

func lock(ctx context.Context, tx database.Tx, scope, tenant, dimension, classKey string) error {
	_, err := tx.Exec(ctx, `
		select pg_advisory_xact_lock(quota_lock_key($1, $2, $3, $4))
	`, scope, tenant, dimension, classKey)
	return err
}

func readOwnerCharges(ctx context.Context, tx database.Tx, owner Owner, forUpdate bool) (result []Charge, err error) {
	if owner.ID == "" {
		return []Charge{}, nil
	}
	query := `
		select charge_path, dimension, class_key, units, coalesce(transfer_key, '')
		from quota_claims
		where tenant = $1 and owner_type = $2 and owner_id = $3
		order by dimension, class_key, charge_path, transfer_key
	`
	if forUpdate {
		query += " for update"
	}
	rows, err := tx.Query(ctx, query, owner.Tenant, owner.Type, owner.ID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var charge Charge
		if err = rows.Scan(&charge.Path, &charge.Dimension, &charge.ClassKey, &charge.Units, &charge.TransferKey); err != nil {
			return nil, err
		}
		result = append(result, charge)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	return result, nil
}

func readTransferClaim(ctx context.Context, tx database.Tx, tenant, transferKey string) (result claimRecord, found bool, err error) {
	err = tx.QueryRow(ctx, `
		select owner_type, owner_id, charge_path, dimension, class_key, units, coalesce(transfer_key, '')
		from quota_claims
		where tenant = $1 and transfer_key = $2
		for update
	`, tenant, transferKey).Scan(
		&result.ownerType,
		&result.ownerID,
		&result.charge.Path,
		&result.charge.Dimension,
		&result.charge.ClassKey,
		&result.charge.Units,
		&result.charge.TransferKey,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return claimRecord{}, false, nil
	}
	if err != nil {
		return claimRecord{}, false, err
	}
	return result, true, nil
}

func uniqueTransferKeys(groups ...[]Charge) []string {
	seen := make(map[string]struct{})
	for _, charges := range groups {
		for _, charge := range charges {
			if charge.TransferKey == "" {
				continue
			}
			seen[charge.TransferKey] = struct{}{}
		}
	}
	keys := make([]string, 0, len(seen))
	for key := range seen {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func sameChargeUnits(existing, requested Charge) bool {
	return existing.Dimension == requested.Dimension &&
		existing.ClassKey == requested.ClassKey &&
		existing.Units == requested.Units
}

func quotaKeys(groups ...[]Charge) []usageKey {
	keysSet := make(map[usageKey]struct{})
	for _, charges := range groups {
		for _, charge := range charges {
			keysSet[usageKey{dimension: charge.Dimension, classKey: charge.ClassKey}] = struct{}{}
		}
	}
	keys := make([]usageKey, 0, len(keysSet))
	for key := range keysSet {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].dimension != keys[j].dimension {
			return keys[i].dimension < keys[j].dimension
		}
		return keys[i].classKey < keys[j].classKey
	})
	return keys
}

func sumByQuotaKey(charges []Charge) (map[usageKey]int64, error) {
	result := make(map[usageKey]int64, len(charges))
	for _, charge := range charges {
		key := usageKey{dimension: charge.Dimension, classKey: charge.ClassKey}
		current := result[key]
		if charge.Units > math.MaxInt64-current {
			return nil, fmt.Errorf("quota usage overflow for %s", charge.Dimension)
		}
		result[key] = current + charge.Units
	}
	return result, nil
}

func readUsage(ctx context.Context, tx database.Tx, tenant string, key usageKey) (int64, error) {
	var result int64
	err := tx.QueryRow(ctx, `
		select used from quota_usage
		where tenant = $1 and dimension = $2 and class_key = $3
	`, tenant, key.dimension, key.classKey).Scan(&result)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil
	}
	return result, err
}

func readLimit(ctx context.Context, tx database.Tx, tenant string, key usageKey) (int64, error) {
	var result int64
	err := tx.QueryRow(ctx, `
		select limit_value from tenant_quota_limits
		where tenant = $1 and dimension = $2 and class_key in ($3, '')
		order by case when class_key = $3 then 0 else 1 end
		limit 1
		for share
	`, tenant, key.dimension, key.classKey).Scan(&result)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil
	}
	return result, err
}

func chargeForKey(charges []Charge, key usageKey) Charge {
	for _, charge := range charges {
		if charge.Dimension == key.dimension && charge.ClassKey == key.classKey {
			return charge
		}
	}
	return Charge{Dimension: key.dimension, ClassKey: key.classKey}
}

func replaceOwnerClaims(ctx context.Context, tx database.Tx, owner Owner, charges []Charge, transfers []claimRecord) error {
	if _, err := tx.Exec(ctx, `
		delete from quota_claims where tenant = $1 and owner_type = $2 and owner_id = $3
	`, owner.Tenant, owner.Type, owner.ID); err != nil {
		return err
	}
	transferredKeys := make(map[string]struct{}, len(transfers))
	for _, transfer := range transfers {
		_, err := tx.Exec(ctx, `
			update quota_claims
			set owner_type = $1, owner_id = $2, charge_path = $3
			where tenant = $4 and owner_type = $5 and owner_id = $6 and transfer_key = $7
		`, owner.Type, owner.ID, transfer.charge.Path, owner.Tenant, transfer.ownerType, transfer.ownerID, transfer.charge.TransferKey)
		if err != nil {
			return err
		}
		transferredKeys[transfer.charge.TransferKey] = struct{}{}
	}
	for _, charge := range charges {
		if _, transferred := transferredKeys[charge.TransferKey]; transferred && charge.TransferKey != "" {
			continue
		}
		if _, err := tx.Exec(ctx, `
			insert into quota_claims (
				tenant, owner_type, owner_id, charge_path, dimension, class_key, units, transfer_key
			) values ($1, $2, $3, $4, $5, $6, $7, nullif($8, ''))
		`, owner.Tenant, owner.Type, owner.ID, charge.Path, charge.Dimension, charge.ClassKey, charge.Units, charge.TransferKey); err != nil {
			return err
		}
	}
	return nil
}
