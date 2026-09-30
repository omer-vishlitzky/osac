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

// Package quota contains the transactional quota ledger used by Fulfillment Service.
package quota

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	DimensionVCPUs              = "vcpus"
	DimensionMemoryGiB          = "memory_gib"
	DimensionGPUs               = "gpus"
	DimensionVolumeCount        = "volumes"
	DimensionStorageGiB         = "storage_gib"
	DimensionDiskImages         = "images"
	DimensionExternalIPs        = "external_ips"
	DimensionNATGateways        = "nat_gateways"
	DimensionVirtualNetworks    = "virtual_networks"
	DimensionBareMetalInstances = "bmaas_instances"
	DimensionCaaSControlPlanes  = "caas_control_planes"
)

// Owner identifies the resource whose quota claims are being replaced.
type Owner struct {
	Tenant string
	Type   string
	ID     string
}

// Charge is one resource's contribution to a tenant quota dimension.
type Charge struct {
	Dimension   string
	ClassKey    string
	Path        string
	Units       int64
	TransferKey string
}

// ExceededError describes a quota dimension that would exceed its limit.
type ExceededError struct {
	Tenant    string
	Dimension string
	ClassKey  string
	Path      string
	Used      int64
	Requested int64
	Limit     int64
}

func (e *ExceededError) Error() string {
	class := ""
	if e.ClassKey != "" {
		class = fmt.Sprintf(" for class '%s'", e.ClassKey)
	}
	path := ""
	if e.Path != "" {
		path = fmt.Sprintf(" (from %s)", e.Path)
	}
	return fmt.Sprintf(
		"quota exceeded for %s%s: current usage %d, request %d, limit %d%s",
		e.Dimension, class, e.Used, e.Requested, e.Limit, path,
	)
}

// GRPCStatus makes quota failures machine-readable while keeping the required
// dimension, limit, and usage values in the human-readable message.
func (e *ExceededError) GRPCStatus() *status.Status {
	result := status.New(codes.ResourceExhausted, e.Error())
	detail := &errdetails.QuotaFailure{
		Violations: []*errdetails.QuotaFailure_Violation{{
			Subject:     fmt.Sprintf("tenants/%s/%s/%s", e.Tenant, e.Dimension, e.ClassKey),
			Description: e.Error(),
		}},
	}
	withDetails, err := result.WithDetails(detail)
	if err != nil {
		return result
	}
	return withDetails
}

type chargeKey struct {
	dimension   string
	classKey    string
	path        string
	transferKey string
}

// NormalizeCharges validates, combines duplicate entries, and sorts a charge
// plan so callers acquire quota locks in a consistent order.
func NormalizeCharges(charges []Charge) ([]Charge, error) {
	combined := make(map[chargeKey]int64, len(charges))
	for _, charge := range charges {
		if charge.Dimension == "" {
			return nil, errors.New("quota charge dimension is required")
		}
		if err := ValidateDimensionClass(charge.Dimension, charge.ClassKey); err != nil {
			return nil, err
		}
		if charge.Path == "" {
			return nil, errors.New("quota charge path is required")
		}
		if charge.Units < 0 {
			return nil, fmt.Errorf("quota charge units cannot be negative for %s", charge.Path)
		}
		if charge.Units == 0 {
			continue
		}
		key := chargeKey{
			dimension:   charge.Dimension,
			classKey:    charge.ClassKey,
			path:        charge.Path,
			transferKey: charge.TransferKey,
		}
		current := combined[key]
		if charge.Units > math.MaxInt64-current {
			return nil, fmt.Errorf("quota charge units overflow for %s", charge.Path)
		}
		combined[key] = current + charge.Units
	}

	result := make([]Charge, 0, len(combined))
	transferOwners := make(map[string]chargeKey)
	for key, units := range combined {
		if key.transferKey != "" {
			if previous, ok := transferOwners[key.transferKey]; ok && previous != key {
				return nil, fmt.Errorf("quota transfer key %q is used by more than one charge", key.transferKey)
			}
			transferOwners[key.transferKey] = key
		}
		result = append(result, Charge{
			Dimension:   key.dimension,
			ClassKey:    key.classKey,
			Path:        key.path,
			Units:       units,
			TransferKey: key.transferKey,
		})
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Dimension != result[j].Dimension {
			return result[i].Dimension < result[j].Dimension
		}
		if result[i].ClassKey != result[j].ClassKey {
			return result[i].ClassKey < result[j].ClassKey
		}
		if result[i].Path != result[j].Path {
			return result[i].Path < result[j].Path
		}
		return strings.Compare(result[i].TransferKey, result[j].TransferKey) < 0
	})
	return result, nil
}
