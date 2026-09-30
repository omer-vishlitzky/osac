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
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	publicv1 "github.com/osac-project/osac/proto/gen/osac/public/v1"
)

const defaultQuotaPageSize int32 = 100

// QuotasServerBuilder configures tenant-visible usage and request operations.
type QuotasServerBuilder struct {
	logger           *slog.Logger
	attributionLogic auth.AttributionLogic
	tenancyLogic     auth.TenancyLogic
	quotaStore       *quota.Store
}

// QuotasServer serves the public quota APIs.
type QuotasServer struct {
	publicv1.UnimplementedQuotasServer
	publicv1.UnimplementedQuotaIncreaseRequestsServer

	logger           *slog.Logger
	attributionLogic auth.AttributionLogic
	tenancyLogic     auth.TenancyLogic
	quotaStore       *quota.Store
}

var (
	_ publicv1.QuotasServer                = (*QuotasServer)(nil)
	_ publicv1.QuotaIncreaseRequestsServer = (*QuotasServer)(nil)
)

func NewQuotasServer() *QuotasServerBuilder {
	return &QuotasServerBuilder{}
}

func (b *QuotasServerBuilder) SetLogger(value *slog.Logger) *QuotasServerBuilder {
	b.logger = value
	return b
}

func (b *QuotasServerBuilder) SetAttributionLogic(value auth.AttributionLogic) *QuotasServerBuilder {
	b.attributionLogic = value
	return b
}

func (b *QuotasServerBuilder) SetTenancyLogic(value auth.TenancyLogic) *QuotasServerBuilder {
	b.tenancyLogic = value
	return b
}

func (b *QuotasServerBuilder) SetQuotaStore(value *quota.Store) *QuotasServerBuilder {
	b.quotaStore = value
	return b
}

func (b *QuotasServerBuilder) Build() (*QuotasServer, error) {
	if b.logger == nil || b.attributionLogic == nil || b.tenancyLogic == nil || b.quotaStore == nil {
		return nil, errors.New("logger, attribution logic, tenancy logic, and quota store are mandatory")
	}
	return &QuotasServer{
		logger:           b.logger,
		attributionLogic: b.attributionLogic,
		tenancyLogic:     b.tenancyLogic,
		quotaStore:       b.quotaStore,
	}, nil
}

func (s *QuotasServer) GetUsage(
	ctx context.Context,
	request *publicv1.QuotasGetUsageRequest,
) (*publicv1.QuotasGetUsageResponse, error) {
	tenant := request.GetTenant()
	if err := s.requireTenantVisibility(ctx, tenant); err != nil {
		return nil, err
	}
	usage, err := s.quotaStore.GetUsage(ctx, tenant)
	if err != nil {
		return nil, s.internalError(ctx, "failed to read quota usage", err)
	}
	entries := make([]*publicv1.QuotaUsageEntry, 0, len(usage))
	for _, item := range usage {
		entries = append(entries, publicQuotaUsage(item))
	}
	return publicv1.QuotasGetUsageResponse_builder{Tenant: tenant, Entries: entries}.Build(), nil
}

func (s *QuotasServer) SetWarningThreshold(
	ctx context.Context,
	request *publicv1.QuotasSetWarningThresholdRequest,
) (*publicv1.QuotasSetWarningThresholdResponse, error) {
	tenant := request.GetTenant()
	if err := s.requireTenantVisibility(ctx, tenant); err != nil {
		return nil, err
	}
	var threshold *int64
	if request.HasWarningThreshold() {
		value := request.GetWarningThreshold()
		threshold = &value
	}
	if err := s.quotaStore.SetWarningThreshold(ctx, tenant, request.GetDimension(), threshold); err != nil {
		return nil, s.translateQuotaError(ctx, "failed to set quota warning threshold", err)
	}
	usage, err := s.quotaStore.GetUsage(ctx, tenant)
	if err != nil {
		return nil, s.internalError(ctx, "failed to read quota usage", err)
	}
	for _, item := range usage {
		if item.Dimension == request.GetDimension() && item.ClassKey == "" {
			return publicv1.QuotasSetWarningThresholdResponse_builder{
				Entry: publicQuotaUsage(item),
			}.Build(), nil
		}
	}
	return nil, s.internalError(ctx, "quota dimension was not seeded", errors.New(request.GetDimension()))
}

func (s *QuotasServer) Create(
	ctx context.Context,
	request *publicv1.QuotaIncreaseRequestsCreateRequest,
) (*publicv1.QuotaIncreaseRequestsCreateResponse, error) {
	object := request.GetObject()
	if object == nil || object.GetMetadata() == nil || object.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "quota increase request metadata and spec are required")
	}
	metadata := object.GetMetadata()
	if object.GetId() != "" || metadata.GetProject() != "" || len(metadata.GetLabels()) != 0 || len(metadata.GetAnnotations()) != 0 {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument,
			"quota increase requests do not accept an ID, project, labels, or annotations")
	}
	if err := s.requireTenantVisibility(ctx, metadata.GetTenant()); err != nil {
		return nil, err
	}
	creator, err := s.attributionLogic.DetermineAssignedCreator(ctx)
	if err != nil {
		return nil, s.internalError(ctx, "failed to identify quota request creator", err)
	}
	created, err := s.quotaStore.CreateIncreaseRequest(ctx, quota.IncreaseRequest{
		Tenant:         metadata.GetTenant(),
		Name:           metadata.GetName(),
		Creator:        creator,
		Dimension:      object.GetSpec().GetDimension(),
		ClassKey:       object.GetSpec().GetClassKey(),
		RequestedLimit: object.GetSpec().GetRequestedLimit(),
		Reason:         object.GetSpec().GetReason(),
	})
	if err != nil {
		return nil, s.translateQuotaError(ctx, "failed to create quota increase request", err)
	}
	return publicv1.QuotaIncreaseRequestsCreateResponse_builder{
		Object: publicQuotaIncreaseRequest(created),
	}.Build(), nil
}

func (s *QuotasServer) Get(
	ctx context.Context,
	request *publicv1.QuotaIncreaseRequestsGetRequest,
) (*publicv1.QuotaIncreaseRequestsGetResponse, error) {
	if err := s.requireTenantVisibility(ctx, request.GetTenant()); err != nil {
		return nil, err
	}
	item, err := s.quotaStore.GetIncreaseRequest(ctx, request.GetTenant(), request.GetId())
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, grpcstatus.Errorf(grpccodes.NotFound, "quota increase request %q not found", request.GetId())
	}
	if err != nil {
		return nil, s.internalError(ctx, "failed to get quota increase request", err)
	}
	return publicv1.QuotaIncreaseRequestsGetResponse_builder{
		Object: publicQuotaIncreaseRequest(item),
	}.Build(), nil
}

func (s *QuotasServer) List(
	ctx context.Context,
	request *publicv1.QuotaIncreaseRequestsListRequest,
) (*publicv1.QuotaIncreaseRequestsListResponse, error) {
	if err := s.requireTenantVisibility(ctx, request.GetTenant()); err != nil {
		return nil, err
	}
	limit, offset := quotaPage(request.HasLimit(), request.GetLimit(), request.HasOffset(), request.GetOffset())
	items, total, err := s.quotaStore.ListIncreaseRequests(ctx, request.GetTenant(), limit, offset)
	if err != nil {
		return nil, s.internalError(ctx, "failed to list quota increase requests", err)
	}
	result := make([]*publicv1.QuotaIncreaseRequest, 0, len(items))
	for _, item := range items {
		result = append(result, publicQuotaIncreaseRequest(item))
	}
	return publicv1.QuotaIncreaseRequestsListResponse_builder{
		Size:  int32(len(result)), //nolint:gosec // The database query is limited by an int32 request value.
		Total: total,
		Items: result,
	}.Build(), nil
}

func (s *QuotasServer) requireTenantVisibility(ctx context.Context, tenant string) error {
	if tenant == "" || tenant == auth.SharedTenant || tenant == auth.SystemTenant {
		return grpcstatus.Error(grpccodes.InvalidArgument, "a tenant-scoped quota tenant is required")
	}
	visibility, err := s.tenancyLogic.DetermineVisibility(ctx)
	if err != nil {
		return s.internalError(ctx, "failed to determine tenant visibility", err)
	}
	if !visibility.IsTenantVisible(tenant) {
		return grpcstatus.Error(grpccodes.PermissionDenied, "tenant is not visible")
	}
	return nil
}

func (s *QuotasServer) translateQuotaError(ctx context.Context, message string, err error) error {
	if code := grpcstatus.Code(err); code != grpccodes.Unknown {
		return err
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "23505":
			return grpcstatus.Error(grpccodes.AlreadyExists, "quota request name already exists")
		case "23503":
			return grpcstatus.Error(grpccodes.NotFound, "tenant was not found")
		}
	}
	return s.internalError(ctx, message, err)
}

func (s *QuotasServer) internalError(ctx context.Context, message string, err error) error {
	s.logger.ErrorContext(ctx, message, slog.Any("error", err))
	return grpcstatus.Error(grpccodes.Internal, message)
}

func publicQuotaUsage(item quota.Usage) *publicv1.QuotaUsageEntry {
	return publicv1.QuotaUsageEntry_builder{
		Dimension:        item.Dimension,
		ClassKey:         item.ClassKey,
		Used:             item.Used,
		Limit:            item.Limit,
		Remaining:        item.Remaining,
		OverLimit:        item.OverLimit,
		WarningThreshold: item.WarningThreshold,
	}.Build()
}

func publicQuotaIncreaseRequest(item quota.IncreaseRequest) *publicv1.QuotaIncreaseRequest {
	return publicv1.QuotaIncreaseRequest_builder{
		Id: item.ID,
		Metadata: publicv1.Metadata_builder{
			Name:              item.Name,
			Tenant:            item.Tenant,
			Creator:           item.Creator,
			CreationTimestamp: timestamppb.New(item.CreatedAt),
		}.Build(),
		Spec: publicv1.QuotaIncreaseRequestSpec_builder{
			Dimension:      item.Dimension,
			ClassKey:       item.ClassKey,
			RequestedLimit: item.RequestedLimit,
			Reason:         item.Reason,
		}.Build(),
		Status: publicv1.QuotaIncreaseRequestStatus_builder{
			State:         publicQuotaRequestState(item.State),
			ReviewMessage: item.ReviewMessage,
			ReviewedAt:    optionalTimestamp(item.ReviewedAt),
		}.Build(),
	}.Build()
}

func publicQuotaRequestState(state string) publicv1.QuotaIncreaseRequestState {
	switch state {
	case quota.RequestPending:
		return publicv1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_PENDING
	case quota.RequestApproved:
		return publicv1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_APPROVED
	case quota.RequestDenied:
		return publicv1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_DENIED
	default:
		return publicv1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_UNSPECIFIED
	}
}

func quotaPage(hasLimit bool, requestedLimit int32, hasOffset bool, requestedOffset int32) (limit, offset int32) {
	limit = defaultQuotaPageSize
	if hasLimit {
		limit = requestedLimit
	}
	if hasOffset {
		offset = requestedOffset
	}
	return
}

func optionalTimestamp(value *time.Time) *timestamppb.Timestamp {
	if value == nil {
		return nil
	}
	return timestamppb.New(*value)
}
