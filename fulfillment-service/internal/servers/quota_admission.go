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

package servers

import (
	"context"
	"errors"
	"log/slog"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/database/dao"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
)

type quotaObject interface {
	dao.Object
	GetMetadata() *privatev1.Metadata
}

// QuotaChargePlanner derives a resource's full quota cost from its prepared candidate.
// For updates, current is the stored object and candidate is the merged replacement.
type QuotaChargePlanner[O quotaObject] func(ctx context.Context, current, candidate O) ([]quota.Charge, error)

// NewQuotaAdmission adapts a resource-specific charge planner to GenericServer's common
// pre-write admission hook.
func NewQuotaAdmission[O quotaObject](
	logger *slog.Logger,
	store *quota.Store,
	ownerType string,
	planner QuotaChargePlanner[O],
) QuotaAdmissionFunc[O] {
	if logger == nil || store == nil || planner == nil {
		return nil
	}
	return func(ctx context.Context, current, candidate O, dryRun bool) error {
		charges, err := planner(ctx, current, candidate)
		if err != nil {
			return err
		}
		metadata := candidate.GetMetadata()
		if metadata == nil {
			return grpcstatus.Error(grpccodes.InvalidArgument, "object metadata is required for quota admission")
		}
		if metadata.GetTenant() == auth.SharedTenant || metadata.GetTenant() == auth.SystemTenant {
			return nil
		}
		err = store.AdmitAndReplace(ctx, quota.Owner{
			Tenant: metadata.GetTenant(),
			Type:   ownerType,
			ID:     candidate.GetId(),
		}, charges, dryRun)
		if err == nil {
			return nil
		}
		var exceeded *quota.ExceededError
		if errors.As(err, &exceeded) {
			return exceeded.GRPCStatus().Err()
		}
		if code := grpcstatus.Code(err); code != grpccodes.Unknown {
			return err
		}
		logger.ErrorContext(ctx, "Failed to evaluate quota admission", slog.Any("error", err))
		return grpcstatus.Error(grpccodes.Internal, "failed to evaluate quota admission")
	}
}
