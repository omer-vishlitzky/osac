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

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

// QuotaAdministrationServerBuilder configures provider-only quota management APIs.
type QuotaAdministrationServerBuilder struct {
	logger           *slog.Logger
	attributionLogic auth.AttributionLogic
	quotaStore       *quota.Store
}

// QuotaAdministrationServer serves provider-only quota management methods.
type QuotaAdministrationServer struct {
	privatev1.UnimplementedQuotaAdministrationServer

	logger           *slog.Logger
	attributionLogic auth.AttributionLogic
	quotaStore       *quota.Store
}

var _ privatev1.QuotaAdministrationServer = (*QuotaAdministrationServer)(nil)

func NewQuotaAdministrationServer() *QuotaAdministrationServerBuilder {
	return &QuotaAdministrationServerBuilder{}
}

func (b *QuotaAdministrationServerBuilder) SetLogger(value *slog.Logger) *QuotaAdministrationServerBuilder {
	b.logger = value
	return b
}

func (b *QuotaAdministrationServerBuilder) SetAttributionLogic(value auth.AttributionLogic) *QuotaAdministrationServerBuilder {
	b.attributionLogic = value
	return b
}

func (b *QuotaAdministrationServerBuilder) SetQuotaStore(value *quota.Store) *QuotaAdministrationServerBuilder {
	b.quotaStore = value
	return b
}

func (b *QuotaAdministrationServerBuilder) Build() (*QuotaAdministrationServer, error) {
	if b.logger == nil || b.attributionLogic == nil || b.quotaStore == nil {
		return nil, errors.New("logger, attribution logic, and quota store are mandatory")
	}
	return &QuotaAdministrationServer{
		logger:           b.logger,
		attributionLogic: b.attributionLogic,
		quotaStore:       b.quotaStore,
	}, nil
}

func (s *QuotaAdministrationServer) ListDefaultLimits(
	ctx context.Context,
	_ *privatev1.ListDefaultLimitsRequest,
) (*privatev1.ListDefaultLimitsResponse, error) {
	limits, err := s.quotaStore.GetDefaultLimits(ctx)
	if err != nil {
		return nil, s.internalError(ctx, "failed to list default quota limits", err)
	}
	items := make([]*privatev1.QuotaLimitEntry, 0, len(limits))
	for _, item := range limits {
		items = append(items, privatev1.QuotaLimitEntry_builder{
			Dimension: item.Dimension,
			ClassKey:  item.ClassKey,
			Limit:     item.Value,
		}.Build())
	}
	return privatev1.ListDefaultLimitsResponse_builder{Items: items}.Build(), nil
}

func (s *QuotaAdministrationServer) SetDefaultLimit(
	ctx context.Context,
	request *privatev1.SetDefaultLimitRequest,
) (*privatev1.SetDefaultLimitResponse, error) {
	actor, err := s.attributionLogic.DetermineAssignedCreator(ctx)
	if err != nil {
		return nil, s.internalError(ctx, "failed to identify quota admin", err)
	}
	if err = s.quotaStore.SetDefaultLimit(ctx, request.GetDimension(), request.GetClassKey(), request.GetLimit(), actor); err != nil {
		return nil, s.translateError(ctx, "failed to set default quota limit", err)
	}
	return privatev1.SetDefaultLimitResponse_builder{Limit: request.GetLimit()}.Build(), nil
}

func (s *QuotaAdministrationServer) SetTenantLimit(
	ctx context.Context,
	request *privatev1.SetTenantLimitRequest,
) (*privatev1.SetTenantLimitResponse, error) {
	actor, err := s.attributionLogic.DetermineAssignedCreator(ctx)
	if err != nil {
		return nil, s.internalError(ctx, "failed to identify quota admin", err)
	}
	if err = s.quotaStore.SetTenantLimit(ctx, request.GetTenant(), request.GetDimension(),
		request.GetClassKey(), request.GetLimit(), actor); err != nil {
		return nil, s.translateError(ctx, "failed to set tenant quota limit", err)
	}
	return privatev1.SetTenantLimitResponse_builder{Limit: request.GetLimit()}.Build(), nil
}

func (s *QuotaAdministrationServer) ReviewIncreaseRequest(
	ctx context.Context,
	request *privatev1.ReviewIncreaseRequestRequest,
) (*privatev1.ReviewIncreaseRequestResponse, error) {
	actor, err := s.attributionLogic.DetermineAssignedCreator(ctx)
	if err != nil {
		return nil, s.internalError(ctx, "failed to identify quota admin", err)
	}
	decision := request.GetDecision()
	if decision != privatev1.QuotaIncreaseDecision_QUOTA_INCREASE_DECISION_APPROVE &&
		decision != privatev1.QuotaIncreaseDecision_QUOTA_INCREASE_DECISION_DENY {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "quota request decision must be approve or deny")
	}
	item, err := s.quotaStore.ReviewIncreaseRequest(
		ctx,
		request.GetId(),
		decision == privatev1.QuotaIncreaseDecision_QUOTA_INCREASE_DECISION_APPROVE,
		request.GetMessage(),
		actor,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, grpcstatus.Errorf(grpccodes.NotFound, "quota increase request %q not found", request.GetId())
	}
	if err != nil {
		return nil, s.translateError(ctx, "failed to review quota increase request", err)
	}
	return privatev1.ReviewIncreaseRequestResponse_builder{
		Object: privateQuotaIncreaseRequest(item),
	}.Build(), nil
}

func (s *QuotaAdministrationServer) ListAuditRecords(
	ctx context.Context,
	request *privatev1.ListAuditRecordsRequest,
) (*privatev1.ListAuditRecordsResponse, error) {
	limit, offset := quotaPage(request.HasLimit(), request.GetLimit(), request.HasOffset(), request.GetOffset())
	items, total, err := s.quotaStore.ListAuditRecords(ctx, request.GetTenant(), limit, offset)
	if err != nil {
		return nil, s.internalError(ctx, "failed to list quota audit records", err)
	}
	result := make([]*privatev1.QuotaAuditRecord, 0, len(items))
	for _, item := range items {
		result = append(result, privatev1.QuotaAuditRecord_builder{
			Tenant:        item.Tenant,
			Dimension:     item.Dimension,
			ClassKey:      item.ClassKey,
			PreviousLimit: item.PreviousLimit,
			NewLimit:      item.NewLimit,
			Actor:         item.Actor,
			ChangedAt:     timestamppb.New(item.ChangedAt),
		}.Build())
	}
	return privatev1.ListAuditRecordsResponse_builder{
		Size:  int32(len(result)), //nolint:gosec // The database query is limited by an int32 request value.
		Total: total,
		Items: result,
	}.Build(), nil
}

func privateQuotaIncreaseRequest(item quota.IncreaseRequest) *privatev1.QuotaIncreaseRequest {
	return privatev1.QuotaIncreaseRequest_builder{
		Id: item.ID,
		Metadata: privatev1.Metadata_builder{
			Name:              item.Name,
			Tenant:            item.Tenant,
			Creator:           item.Creator,
			CreationTimestamp: timestamppb.New(item.CreatedAt),
		}.Build(),
		Spec: privatev1.QuotaIncreaseRequestSpec_builder{
			Dimension:      item.Dimension,
			ClassKey:       item.ClassKey,
			RequestedLimit: item.RequestedLimit,
			Reason:         item.Reason,
		}.Build(),
		Status: privatev1.QuotaIncreaseRequestStatus_builder{
			State:         privateQuotaRequestState(item.State),
			ReviewMessage: item.ReviewMessage,
			ReviewedAt:    optionalTimestamp(item.ReviewedAt),
			ReviewedBy:    item.ReviewedBy,
		}.Build(),
	}.Build()
}

func privateQuotaRequestState(state string) privatev1.QuotaIncreaseRequestState {
	switch state {
	case quota.RequestPending:
		return privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_PENDING
	case quota.RequestApproved:
		return privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_APPROVED
	case quota.RequestDenied:
		return privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_DENIED
	default:
		return privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_UNSPECIFIED
	}
}

func (s *QuotaAdministrationServer) translateError(ctx context.Context, message string, err error) error {
	if code := grpcstatus.Code(err); code != grpccodes.Unknown {
		return err
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "23505":
			return grpcstatus.Error(grpccodes.AlreadyExists, "quota limit already exists")
		case "23503":
			return grpcstatus.Error(grpccodes.NotFound, "tenant was not found")
		}
	}
	return s.internalError(ctx, message, err)
}

func (s *QuotaAdministrationServer) internalError(ctx context.Context, message string, err error) error {
	s.logger.ErrorContext(ctx, message, slog.Any("error", err))
	return grpcstatus.Error(grpccodes.Internal, message)
}
