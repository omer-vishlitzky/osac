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

	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/database/dao"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

var _ = Describe("Quota charge planners", func() {
	It("charges a Cluster control plane, BM worker sets, and automatic ExternalIPs", func() {
		cluster := privatev1.Cluster_builder{
			Metadata: privatev1.Metadata_builder{Tenant: testTenant}.Build(),
			Spec: privatev1.ClusterSpec_builder{
				NodeSets: map[string]*privatev1.ClusterNodeSet{
					"gpu": privatev1.ClusterNodeSet_builder{
						Size:                  new(int32(3)),
						BaremetalInstanceType: privatev1.BareMetalInstanceTypeLocalReference_builder{Id: "bmit-gpu"}.Build(),
					}.Build(),
				},
				AutoExternalIpAttachment: new(true),
			}.Build(),
		}.Build()

		charges, err := planClusterQuotaCharges(ctx, nil, cluster)
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(ConsistOf(
			quota.Charge{Dimension: quota.DimensionCaaSControlPlanes, Path: "cluster.control_plane", Units: 1},
			quota.Charge{Dimension: quota.DimensionBareMetalInstances, ClassKey: "bmit:bmit-gpu", Path: "spec.node_sets.gpu", Units: 3},
			quota.Charge{Dimension: quota.DimensionExternalIPs, Path: "spec.auto_external_ip_attachment", Units: 2},
		))
	})

	It("requires CaaS node sets to be resolved before quota admission", func() {
		cluster := privatev1.Cluster_builder{
			Metadata: privatev1.Metadata_builder{Tenant: testTenant}.Build(),
			Spec:     &privatev1.ClusterSpec{},
		}.Build()

		_, err := planClusterQuotaCharges(ctx, nil, cluster)
		Expect(status.Code(err)).To(Equal(codes.FailedPrecondition))
	})

	It("charges VM type resources, tiered disks, and automatic ExternalIP", func() {
		instanceTypes, err := dao.NewGenericDAO[*privatev1.InstanceType]().
			SetLogger(logger).
			SetTenancyLogic(tenancy).
			Build()
		Expect(err).ToNot(HaveOccurred())
		_, err = instanceTypes.Create().SetObject(privatev1.InstanceType_builder{
			Id: "instance-type-small",
			Metadata: privatev1.Metadata_builder{
				Name:   "small",
				Tenant: "shared",
			}.Build(),
			Spec: privatev1.InstanceTypeSpec_builder{
				Vcpus:     4,
				MemoryGib: 16,
				Gpu: privatev1.GpuSpec_builder{
					ResourceName: "nvidia.com/a100",
					Count:        2,
				}.Build(),
			}.Build(),
		}.Build()).Do(ctx)
		Expect(err).ToNot(HaveOccurred())

		instance := privatev1.ComputeInstance_builder{
			Metadata: privatev1.Metadata_builder{Name: "vm-one", Tenant: testTenant}.Build(),
			Spec: privatev1.ComputeInstanceSpec_builder{
				InstanceType: privatev1.InstanceTypeReference_builder{Id: "instance-type-small"}.Build(),
				BootDisk: privatev1.ComputeInstanceDisk_builder{
					SizeGib:     new(int32(50)),
					StorageTier: privatev1.StorageTierReference_builder{Name: "gold"}.Build(),
				}.Build(),
				AdditionalDisks: []*privatev1.ComputeInstanceDisk{
					privatev1.ComputeInstanceDisk_builder{
						SizeGib:     new(int32(100)),
						StorageTier: privatev1.StorageTierReference_builder{Name: "gold"}.Build(),
					}.Build(),
				},
				AutoExternalIpAttachment: new(true),
			}.Build(),
		}.Build()

		charges, err := planComputeInstanceQuotaCharges(ctx, nil, instance, instanceTypes)
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(ConsistOf(
			quota.Charge{Dimension: quota.DimensionVCPUs, Path: "spec.instance_type", Units: 4},
			quota.Charge{Dimension: quota.DimensionMemoryGiB, Path: "spec.instance_type", Units: 16},
			quota.Charge{Dimension: quota.DimensionGPUs, ClassKey: "nvidia.com/a100", Path: "spec.instance_type.gpu", Units: 2},
			quota.Charge{Dimension: quota.DimensionVolumeCount, Path: "spec.boot_disk", Units: 1, TransferKey: "vm-one-root-disk:volumes"},
			quota.Charge{Dimension: quota.DimensionStorageGiB, ClassKey: "gold", Path: "spec.boot_disk", Units: 50, TransferKey: "vm-one-root-disk:storage_gib"},
			quota.Charge{Dimension: quota.DimensionVolumeCount, Path: "spec.additional_disks[0]", Units: 1, TransferKey: "vm-one-disk-1:volumes"},
			quota.Charge{Dimension: quota.DimensionStorageGiB, ClassKey: "gold", Path: "spec.additional_disks[0]", Units: 100, TransferKey: "vm-one-disk-1:storage_gib"},
			quota.Charge{Dimension: quota.DimensionExternalIPs, Path: "spec.auto_external_ip_attachment", Units: 1},
		))

		instance.SetStatus(privatev1.ComputeInstanceStatus_builder{
			State: privatev1.ComputeInstanceState_COMPUTE_INSTANCE_STATE_FAILED,
		}.Build())
		failedCharges, err := planComputeInstanceQuotaCharges(ctx, instance, instance, instanceTypes)
		Expect(err).ToNot(HaveOccurred())
		Expect(failedCharges).To(Equal(charges), "FAILED resources remain charged until the object is deleted")
	})

	It("charges a BM instance by BMIT or template HostType", func() {
		templates, err := dao.NewGenericDAO[*privatev1.BareMetalInstanceTemplate]().
			SetLogger(logger).
			SetTenancyLogic(tenancy).
			Build()
		Expect(err).ToNot(HaveOccurred())
		_, err = templates.Create().SetObject(privatev1.BareMetalInstanceTemplate_builder{
			Id:       "bm-template",
			Metadata: privatev1.Metadata_builder{Name: "bm-template", Tenant: "shared"}.Build(),
			HostType: "host-medium",
		}.Build()).Do(ctx)
		Expect(err).ToNot(HaveOccurred())

		instance := privatev1.BareMetalInstance_builder{
			Metadata: privatev1.Metadata_builder{Name: "bm-one", Tenant: testTenant}.Build(),
			Spec: privatev1.BareMetalInstanceSpec_builder{
				Template: privatev1.BareMetalInstanceTemplateReference_builder{Id: "bm-template"}.Build(),
			}.Build(),
		}.Build()
		charges, err := planBareMetalInstanceQuotaCharges(ctx, nil, instance, templates)
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(ConsistOf(quota.Charge{
			Dimension: quota.DimensionBareMetalInstances,
			ClassKey:  "host_type:host-medium",
			Path:      "spec.instance_type",
			Units:     1,
		}))
	})

	It("charges Volume count and capacity by storage tier", func() {
		volume := privatev1.Volume_builder{
			Metadata: privatev1.Metadata_builder{Name: "vm-one-root-disk", Tenant: testTenant}.Build(),
			Spec: privatev1.VolumeSpec_builder{
				StorageTier: "gold",
				SizeGib:     50,
			}.Build(),
		}.Build()
		charges, err := planVolumeQuotaCharges(context.Background(), nil, volume)
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(ConsistOf(
			quota.Charge{Dimension: quota.DimensionVolumeCount, Path: "volume", Units: 1, TransferKey: "vm-one-root-disk:volumes"},
			quota.Charge{Dimension: quota.DimensionStorageGiB, ClassKey: "gold", Path: "spec.size_gib", Units: 50, TransferKey: "vm-one-root-disk:storage_gib"},
		))
	})

	It("does not charge shared DiskImages to a tenant", func() {
		image := privatev1.DiskImage_builder{
			Metadata: privatev1.Metadata_builder{Tenant: auth.SharedTenant}.Build(),
		}.Build()
		charges, err := planDiskImageQuotaCharges(context.Background(), nil, image)
		Expect(err).ToNot(HaveOccurred())
		Expect(charges).To(BeEmpty())
	})
})
