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
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/database"
	"github.com/osac-project/osac/fulfillment-service/internal/uuid"
)

const (
	RequestPending  = "PENDING"
	RequestApproved = "APPROVED"
	RequestDenied   = "DENIED"
)

// IncreaseRequest is a tenant's request for a higher quota limit.
type IncreaseRequest struct {
	ID             string
	Tenant         string
	Name           string
	Creator        string
	Dimension      string
	ClassKey       string
	RequestedLimit int64
	Reason         string
	State          string
	ReviewMessage  string
	ReviewedBy     string
	CreatedAt      time.Time
	ReviewedAt     *time.Time
}

// CreateIncreaseRequest saves a pending tenant request if it asks for more than
// the currently effective limit.
func (s *Store) CreateIncreaseRequest(ctx context.Context, request IncreaseRequest) (result IncreaseRequest, err error) {
	if request.Tenant == "" || request.Name == "" || request.Creator == "" {
		return result, status.Error(codes.InvalidArgument, "quota request tenant, name, and creator are required")
	}
	if err := ValidateDimensionClass(request.Dimension, request.ClassKey); err != nil {
		return result, status.Error(codes.InvalidArgument, err.Error())
	}
	if request.RequestedLimit <= 0 || strings.TrimSpace(request.Reason) == "" {
		return result, status.Error(codes.InvalidArgument, "quota request must include a positive limit and a reason")
	}

	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return result, err
	}
	defer tx.ReportError(&err)

	key := usageKey{
		dimension: request.Dimension,
		classKey:  request.ClassKey,
	}
	if err = lock(ctx, tx, "usage", request.Tenant, key.dimension, key.classKey); err != nil {
		return result, err
	}
	currentLimit, err := readLimit(ctx, tx, request.Tenant, key)
	if err != nil {
		return result, err
	}
	if request.RequestedLimit <= currentLimit {
		return result, status.Errorf(codes.InvalidArgument,
			"requested limit must be greater than the current limit %d", currentLimit)
	}

	request.ID = uuid.New()
	err = tx.QueryRow(ctx, `
		insert into quota_increase_requests (
			id, tenant, name, creator, dimension, class_key, requested_limit, reason
		) values ($1, $2, $3, $4, $5, $6, $7, $8)
		returning creation_timestamp
	`, request.ID, request.Tenant, request.Name, request.Creator, request.Dimension,
		request.ClassKey, request.RequestedLimit, strings.TrimSpace(request.Reason)).Scan(&request.CreatedAt)
	if err != nil {
		return result, err
	}
	request.State = RequestPending
	return request, nil
}

// GetIncreaseRequest returns a request belonging to the requested tenant.
func (s *Store) GetIncreaseRequest(ctx context.Context, tenant, id string) (IncreaseRequest, error) {
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return IncreaseRequest{}, err
	}
	return scanIncreaseRequest(tx.QueryRow(ctx, `
		select id, tenant, name, creator, dimension, class_key, requested_limit,
		       reason, state, review_message, reviewed_by, creation_timestamp, reviewed_at
		from quota_increase_requests
		where tenant = $1 and id = $2
	`, tenant, id))
}

// ListIncreaseRequests returns newest requests for a tenant and their total count.
func (s *Store) ListIncreaseRequests(
	ctx context.Context,
	tenant string,
	limit int32,
	offset int32,
) (result []IncreaseRequest, total int32, err error) {
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return nil, 0, err
	}
	err = tx.QueryRow(ctx, `
		select count(*) from quota_increase_requests where tenant = $1
	`, tenant).Scan(&total)
	if err != nil {
		return nil, 0, err
	}
	rows, err := tx.Query(ctx, `
		select id, tenant, name, creator, dimension, class_key, requested_limit,
		       reason, state, review_message, reviewed_by, creation_timestamp, reviewed_at
		from quota_increase_requests
		where tenant = $1
		order by creation_timestamp desc, id
		limit $2 offset $3
	`, tenant, limit, offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		item, scanErr := scanIncreaseRequest(rows)
		if scanErr != nil {
			return nil, 0, scanErr
		}
		result = append(result, item)
	}
	return result, total, rows.Err()
}

// ReviewIncreaseRequest atomically records a provider decision and, on approval,
// applies the requested limit and audit record.
func (s *Store) ReviewIncreaseRequest(
	ctx context.Context,
	id string,
	approve bool,
	reviewMessage string,
	actor string,
) (result IncreaseRequest, err error) {
	if id == "" || actor == "" {
		return result, status.Error(codes.InvalidArgument, "quota request ID and reviewer are required")
	}
	tx, err := database.TxFromContext(ctx)
	if err != nil {
		return result, err
	}
	defer tx.ReportError(&err)

	request, err := scanIncreaseRequest(tx.QueryRow(ctx, `
		select id, tenant, name, creator, dimension, class_key, requested_limit,
		       reason, state, review_message, reviewed_by, creation_timestamp, reviewed_at
		from quota_increase_requests
		where id = $1
		for update
	`, id))
	if err != nil {
		return result, err
	}
	if request.State != RequestPending {
		return result, status.Errorf(codes.FailedPrecondition,
			"quota increase request %q has already been reviewed", id)
	}
	if approve {
		key := usageKey{dimension: request.Dimension, classKey: request.ClassKey}
		if err = lock(ctx, tx, "usage", request.Tenant, key.dimension, key.classKey); err != nil {
			return result, err
		}
		currentLimit, limitErr := readLimit(ctx, tx, request.Tenant, key)
		if limitErr != nil {
			return result, limitErr
		}
		if request.RequestedLimit <= currentLimit {
			return result, status.Errorf(codes.FailedPrecondition,
				"requested limit %d is no longer greater than the current limit %d",
				request.RequestedLimit, currentLimit)
		}
		if err = s.SetTenantLimit(ctx, request.Tenant, request.Dimension, request.ClassKey, request.RequestedLimit, actor); err != nil {
			return result, err
		}
		request.State = RequestApproved
	} else {
		request.State = RequestDenied
	}
	request.ReviewMessage = reviewMessage
	request.ReviewedBy = actor
	now := time.Now().UTC()
	request.ReviewedAt = &now
	err = tx.QueryRow(ctx, `
		update quota_increase_requests
		set state = $2, review_message = $3, reviewed_by = $4, reviewed_at = now()
		where id = $1
		returning reviewed_at
	`, id, request.State, reviewMessage, actor).Scan(&now)
	if err != nil {
		return result, err
	}
	request.ReviewedAt = &now
	return request, nil
}

func scanIncreaseRequest(row pgx.Row) (result IncreaseRequest, err error) {
	var reviewedAt pgtype.Timestamptz
	err = row.Scan(
		&result.ID,
		&result.Tenant,
		&result.Name,
		&result.Creator,
		&result.Dimension,
		&result.ClassKey,
		&result.RequestedLimit,
		&result.Reason,
		&result.State,
		&result.ReviewMessage,
		&result.ReviewedBy,
		&result.CreatedAt,
		&reviewedAt,
	)
	if err != nil {
		return IncreaseRequest{}, err
	}
	if reviewedAt.Valid {
		result.ReviewedAt = &reviewedAt.Time
	}
	return result, nil
}
