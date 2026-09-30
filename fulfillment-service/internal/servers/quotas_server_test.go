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
	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"
	"go.uber.org/mock/gomock"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
	publicv1 "github.com/osac-project/osac/proto/gen/osac/public/v1"
)

var _ = Describe("Quota APIs", func() {
	var (
		store     *quota.Store
		publicAPI *QuotasServer
		adminAPI  privatev1.QuotaAdministrationServer
	)

	BeforeEach(func() {
		store = quota.NewStore()
		var err error
		publicAPI, err = NewQuotasServer().
			SetLogger(logger).
			SetAttributionLogic(attribution).
			SetTenancyLogic(tenancy).
			SetQuotaStore(store).
			Build()
		Expect(err).ToNot(HaveOccurred())

		adminAPI, err = NewQuotaAdministrationServer().
			SetLogger(logger).
			SetAttributionLogic(attribution).
			SetQuotaStore(store).
			Build()
		Expect(err).ToNot(HaveOccurred())
	})

	It("shows tenant usage and warning settings, and tracks approved and denied requests", func() {
		_, err := suiteTx.Exec(ctx, `
			update tenant_quota_limits set limit_value = 5
			where tenant = $1 and dimension = 'vcpus' and class_key = ''
		`, testTenant)
		Expect(err).ToNot(HaveOccurred())

		usageResponse, err := publicAPI.GetUsage(ctx, publicv1.QuotasGetUsageRequest_builder{
			Tenant: testTenant,
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(usageResponse.GetTenant()).To(Equal(testTenant))
		Expect(usageFor(usageResponse.GetEntries(), "vcpus", "").GetLimit()).To(Equal(int64(5)))

		threshold := int64(2)
		thresholdResponse, err := publicAPI.SetWarningThreshold(ctx, publicv1.QuotasSetWarningThresholdRequest_builder{
			Tenant:           testTenant,
			Dimension:        "vcpus",
			WarningThreshold: &threshold,
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(thresholdResponse.GetEntry().GetWarningThreshold()).To(Equal(int64(2)))
		Expect(thresholdResponse.GetEntry().HasWarningThreshold()).To(BeTrue())

		createRequest := func(name string, requestedLimit int64) *publicv1.QuotaIncreaseRequest {
			return publicv1.QuotaIncreaseRequest_builder{
				Metadata: publicv1.Metadata_builder{Name: name, Tenant: testTenant}.Build(),
				Spec: publicv1.QuotaIncreaseRequestSpec_builder{
					Dimension:      "vcpus",
					RequestedLimit: requestedLimit,
					Reason:         "Growing the team workload",
				}.Build(),
			}.Build()
		}
		created, err := publicAPI.Create(ctx, publicv1.QuotaIncreaseRequestsCreateRequest_builder{
			Object: createRequest("more-vcpus", 8),
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(created.GetObject().GetId()).NotTo(BeEmpty())
		Expect(created.GetObject().GetStatus().GetState()).To(Equal(
			publicv1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_PENDING,
		))

		requests, err := publicAPI.List(ctx, publicv1.QuotaIncreaseRequestsListRequest_builder{
			Tenant: testTenant,
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(requests.GetTotal()).To(Equal(int32(1)))

		approved, err := adminAPI.ReviewIncreaseRequest(ctx, privatev1.ReviewIncreaseRequestRequest_builder{
			Id:       created.GetObject().GetId(),
			Decision: privatev1.QuotaIncreaseDecision_QUOTA_INCREASE_DECISION_APPROVE,
			Message:  "Approved",
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(approved.GetObject().GetStatus().GetState()).To(Equal(
			privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_APPROVED,
		))
		Expect(approved.GetObject().GetStatus().GetReviewedBy()).To(Equal("system"))

		usageResponse, err = publicAPI.GetUsage(ctx, publicv1.QuotasGetUsageRequest_builder{
			Tenant: testTenant,
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(usageFor(usageResponse.GetEntries(), "vcpus", "").GetLimit()).To(Equal(int64(8)))

		requestToDeny, err := publicAPI.Create(ctx, publicv1.QuotaIncreaseRequestsCreateRequest_builder{
			Object: createRequest("more-vcpus-again", 12),
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		denied, err := adminAPI.ReviewIncreaseRequest(ctx, privatev1.ReviewIncreaseRequestRequest_builder{
			Id:       requestToDeny.GetObject().GetId(),
			Decision: privatev1.QuotaIncreaseDecision_QUOTA_INCREASE_DECISION_DENY,
			Message:  "Not in this contract",
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(denied.GetObject().GetStatus().GetState()).To(Equal(
			privatev1.QuotaIncreaseRequestState_QUOTA_INCREASE_REQUEST_STATE_DENIED,
		))

		audit, err := adminAPI.ListAuditRecords(ctx, privatev1.ListAuditRecordsRequest_builder{
			Tenant: testTenant,
		}.Build())
		Expect(err).ToNot(HaveOccurred())
		Expect(audit.GetTotal()).To(Equal(int32(1)))
		Expect(audit.GetItems()[0].GetPreviousLimit()).To(Equal(int64(5)))
		Expect(audit.GetItems()[0].GetNewLimit()).To(Equal(int64(8)))
	})

	It("rejects quota requests for dimensions not enforced by admission", func() {
		request := publicv1.QuotaIncreaseRequest_builder{
			Metadata: publicv1.Metadata_builder{Name: "image-capacity", Tenant: testTenant}.Build(),
			Spec: publicv1.QuotaIncreaseRequestSpec_builder{
				Dimension:      "image_capacity_gib",
				RequestedLimit: 100,
				Reason:         "Image storage",
			}.Build(),
		}.Build()
		_, err := publicAPI.Create(ctx, publicv1.QuotaIncreaseRequestsCreateRequest_builder{Object: request}.Build())
		Expect(status.Code(err)).To(Equal(codes.InvalidArgument))
	})

	It("denies quota usage queries for tenants outside the caller visibility", func() {
		visibility, err := auth.NewVisibility().AddVisibleTenant("other-tenant").Build()
		Expect(err).ToNot(HaveOccurred())
		restrictedTenancy := auth.NewMockTenancyLogic(gomock.NewController(GinkgoT()))
		restrictedTenancy.EXPECT().DetermineVisibility(gomock.Any()).Return(visibility, nil).AnyTimes()
		restrictedAPI, err := NewQuotasServer().
			SetLogger(logger).
			SetAttributionLogic(attribution).
			SetTenancyLogic(restrictedTenancy).
			SetQuotaStore(store).
			Build()
		Expect(err).ToNot(HaveOccurred())
		_, err = restrictedAPI.GetUsage(ctx, publicv1.QuotasGetUsageRequest_builder{
			Tenant: testTenant,
		}.Build())
		Expect(status.Code(err)).To(Equal(codes.PermissionDenied))
	})
})

func usageFor(entries []*publicv1.QuotaUsageEntry, dimension, classKey string) *publicv1.QuotaUsageEntry {
	for _, entry := range entries {
		if entry.GetDimension() == dimension && entry.GetClassKey() == classKey {
			return entry
		}
	}
	Fail("quota usage entry not found")
	return nil
}
