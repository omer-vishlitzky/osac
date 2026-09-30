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
	"fmt"

	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/database/dao"
	"github.com/osac-project/osac/fulfillment-service/internal/quota"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

func planClusterQuotaCharges(
	_ context.Context,
	_ *privatev1.Cluster,
	cluster *privatev1.Cluster,
) ([]quota.Charge, error) {
	if cluster == nil || cluster.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "cluster spec is required for quota admission")
	}

	charges := []quota.Charge{{
		Dimension: quota.DimensionCaaSControlPlanes,
		Path:      "cluster.control_plane",
		Units:     1,
	}}
	nodeSets := cluster.GetSpec().GetNodeSets()
	if len(nodeSets) == 0 {
		return nil, grpcstatus.Error(grpccodes.FailedPrecondition,
			"cluster worker node sets must be resolved before quota admission")
	}
	for name, nodeSet := range nodeSets {
		if nodeSet == nil {
			return nil, grpcstatus.Errorf(grpccodes.InvalidArgument, "spec.node_sets.%s is required", name)
		}
		classKey := bareMetalNodeClass(nodeSet)
		if classKey == "" {
			return nil, grpcstatus.Errorf(
				grpccodes.FailedPrecondition,
				"spec.node_sets.%s must identify a bare-metal resource class for quota admission",
				name,
			)
		}
		size := int64(nodeSet.GetSize())
		if size <= 0 {
			return nil, grpcstatus.Errorf(grpccodes.InvalidArgument, "spec.node_sets.%s.size must be greater than zero", name)
		}
		charges = append(charges, quota.Charge{
			Dimension: quota.DimensionBareMetalInstances,
			ClassKey:  classKey,
			Path:      "spec.node_sets." + name,
			Units:     size,
		})
	}
	if cluster.GetSpec().GetAutoExternalIpAttachment() {
		charges = append(charges, quota.Charge{
			Dimension: quota.DimensionExternalIPs,
			Path:      "spec.auto_external_ip_attachment",
			Units:     2,
		})
	}
	return quota.NormalizeCharges(charges)
}

func planComputeInstanceQuotaCharges(
	ctx context.Context,
	_ *privatev1.ComputeInstance,
	instance *privatev1.ComputeInstance,
	instanceTypes *dao.GenericDAO[*privatev1.InstanceType],
) ([]quota.Charge, error) {
	if instance == nil || instance.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "compute instance spec is required for quota admission")
	}
	instanceType := instance.GetSpec().GetInstanceType()
	if instanceType == nil || instanceType.GetId() == "" {
		return nil, grpcstatus.Error(grpccodes.FailedPrecondition,
			"spec.instance_type must be resolved before quota admission")
	}
	response, err := instanceTypes.Get().SetId(instanceType.GetId()).Do(ctx)
	if err != nil {
		return nil, grpcstatus.Errorf(grpccodes.FailedPrecondition,
			"failed to resolve instance type for quota admission: %v", err)
	}
	resolved := response.GetObject()
	if resolved == nil || resolved.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.Internal, "resolved instance type has no spec")
	}

	charges := []quota.Charge{
		{Dimension: quota.DimensionVCPUs, Path: "spec.instance_type", Units: int64(resolved.GetSpec().GetVcpus())},
		{Dimension: quota.DimensionMemoryGiB, Path: "spec.instance_type", Units: int64(resolved.GetSpec().GetMemoryGib())},
	}
	if gpu := resolved.GetSpec().GetGpu(); gpu != nil {
		if gpu.GetResourceName() == "" {
			return nil, grpcstatus.Error(grpccodes.FailedPrecondition,
				"instance type GPU resource name is required for quota admission")
		}
		charges = append(charges, quota.Charge{
			Dimension: quota.DimensionGPUs,
			ClassKey:  gpu.GetResourceName(),
			Path:      "spec.instance_type.gpu",
			Units:     int64(gpu.GetCount()),
		})
	}

	if err := appendComputeDiskCharges(&charges, instance); err != nil {
		return nil, err
	}
	if instance.GetSpec().GetAutoExternalIpAttachment() {
		charges = append(charges, quota.Charge{
			Dimension: quota.DimensionExternalIPs,
			Path:      "spec.auto_external_ip_attachment",
			Units:     1,
		})
	}
	return quota.NormalizeCharges(charges)
}

func appendComputeDiskCharges(charges *[]quota.Charge, instance *privatev1.ComputeInstance) error {
	metadataName := instance.GetMetadata().GetName()
	if metadataName == "" {
		return grpcstatus.Error(grpccodes.InvalidArgument, "metadata.name is required for disk quota admission")
	}
	add := func(path, pvcName string, disk *privatev1.ComputeInstanceDisk) error {
		if disk == nil {
			return nil
		}
		tier := disk.GetStorageTier()
		if tier == nil {
			return grpcstatus.Errorf(grpccodes.FailedPrecondition,
				"%s.storage_tier is required for quota admission", path)
		}
		classKey := tier.GetName()
		if classKey == "" {
			return grpcstatus.Errorf(grpccodes.FailedPrecondition,
				"%s.storage_tier must be resolved before quota admission", path)
		}
		units := int64(disk.GetSizeGib())
		if units <= 0 {
			return grpcstatus.Errorf(grpccodes.FailedPrecondition,
				"%s.size_gib must be resolved before quota admission", path)
		}
		*charges = append(*charges, quota.Charge{
			Dimension:   quota.DimensionVolumeCount,
			Path:        path,
			Units:       1,
			TransferKey: volumeTransferKey(pvcName, quota.DimensionVolumeCount),
		}, quota.Charge{
			Dimension:   quota.DimensionStorageGiB,
			ClassKey:    classKey,
			Path:        path,
			Units:       units,
			TransferKey: volumeTransferKey(pvcName, quota.DimensionStorageGiB),
		})
		return nil
	}

	if err := add("spec.boot_disk", metadataName+"-root-disk", instance.GetSpec().GetBootDisk()); err != nil {
		return err
	}
	for index, disk := range instance.GetSpec().GetAdditionalDisks() {
		if err := add(
			fmt.Sprintf("spec.additional_disks[%d]", index),
			fmt.Sprintf("%s-disk-%d", metadataName, index+1),
			disk,
		); err != nil {
			return err
		}
	}
	return nil
}

func planBareMetalInstanceQuotaCharges(
	ctx context.Context,
	_ *privatev1.BareMetalInstance,
	instance *privatev1.BareMetalInstance,
	templates *dao.GenericDAO[*privatev1.BareMetalInstanceTemplate],
) ([]quota.Charge, error) {
	if instance == nil || instance.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "bare metal instance spec is required for quota admission")
	}
	classKey := bareMetalInstanceClass(instance.GetSpec().GetInstanceType())
	if classKey == "" {
		templateRef := instance.GetSpec().GetTemplate()
		if templateRef != nil && templateRef.GetId() != "" {
			response, err := templates.Get().SetId(templateRef.GetId()).Do(ctx)
			if err != nil {
				return nil, grpcstatus.Errorf(grpccodes.FailedPrecondition,
					"failed to resolve bare-metal template for quota admission: %v", err)
			}
			if template := response.GetObject(); template != nil {
				classKey = resourceClass("host_type", template.GetHostType())
			}
		}
	}
	if classKey == "" {
		return nil, grpcstatus.Error(grpccodes.FailedPrecondition,
			"bare metal instance type must be resolved before quota admission")
	}
	charges := []quota.Charge{{
		Dimension: quota.DimensionBareMetalInstances,
		ClassKey:  classKey,
		Path:      "spec.instance_type",
		Units:     1,
	}}
	if instance.GetSpec().GetAutoExternalIpAttachment() {
		charges = append(charges, quota.Charge{
			Dimension: quota.DimensionExternalIPs,
			Path:      "spec.auto_external_ip_attachment",
			Units:     1,
		})
	}
	return quota.NormalizeCharges(charges)
}

func planVolumeQuotaCharges(
	_ context.Context,
	_ *privatev1.Volume,
	volume *privatev1.Volume,
) ([]quota.Charge, error) {
	if volume == nil || volume.GetSpec() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "volume spec is required for quota admission")
	}
	if volume.GetSpec().GetStorageTier() == "" {
		return nil, grpcstatus.Error(grpccodes.FailedPrecondition, "spec.storage_tier is required for quota admission")
	}
	if volume.GetSpec().GetSizeGib() <= 0 {
		return nil, grpcstatus.Error(grpccodes.FailedPrecondition, "spec.size_gib is required for quota admission")
	}
	if volume.GetMetadata().GetName() == "" {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "metadata.name is required for quota admission")
	}
	return quota.NormalizeCharges([]quota.Charge{
		{
			Dimension:   quota.DimensionVolumeCount,
			Path:        "volume",
			Units:       1,
			TransferKey: volumeTransferKey(volume.GetMetadata().GetName(), quota.DimensionVolumeCount),
		},
		{
			Dimension:   quota.DimensionStorageGiB,
			ClassKey:    volume.GetSpec().GetStorageTier(),
			Path:        "spec.size_gib",
			Units:       volume.GetSpec().GetSizeGib(),
			TransferKey: volumeTransferKey(volume.GetMetadata().GetName(), quota.DimensionStorageGiB),
		},
	})
}

// The claim ledger allows one owner per transfer key, so each PVC dimension transfers independently.
func volumeTransferKey(pvcName, dimension string) string {
	return pvcName + ":" + dimension
}

func planDiskImageQuotaCharges(
	_ context.Context,
	_ *privatev1.DiskImage,
	image *privatev1.DiskImage,
) ([]quota.Charge, error) {
	if image == nil || image.GetMetadata() == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "disk image metadata is required for quota admission")
	}
	if image.GetMetadata().GetTenant() == auth.SharedTenant || image.GetMetadata().GetTenant() == auth.SystemTenant {
		return nil, nil
	}
	return quota.NormalizeCharges([]quota.Charge{{
		Dimension: quota.DimensionDiskImages,
		Path:      "disk_image",
		Units:     1,
	}})
}

func planExternalIPQuotaCharges(
	_ context.Context,
	_ *privatev1.ExternalIP,
	ip *privatev1.ExternalIP,
) ([]quota.Charge, error) {
	if ip == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "external IP is required for quota admission")
	}
	return quota.NormalizeCharges([]quota.Charge{{
		Dimension: quota.DimensionExternalIPs,
		Path:      "external_ip",
		Units:     1,
	}})
}

func planNATGatewayQuotaCharges(
	_ context.Context,
	_ *privatev1.NATGateway,
	gateway *privatev1.NATGateway,
) ([]quota.Charge, error) {
	if gateway == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "NAT gateway is required for quota admission")
	}
	return quota.NormalizeCharges([]quota.Charge{{
		Dimension: quota.DimensionNATGateways,
		Path:      "nat_gateway",
		Units:     1,
	}})
}

func planVirtualNetworkQuotaCharges(
	_ context.Context,
	_ *privatev1.VirtualNetwork,
	network *privatev1.VirtualNetwork,
) ([]quota.Charge, error) {
	if network == nil {
		return nil, grpcstatus.Error(grpccodes.InvalidArgument, "virtual network is required for quota admission")
	}
	return quota.NormalizeCharges([]quota.Charge{{
		Dimension: quota.DimensionVirtualNetworks,
		Path:      "virtual_network",
		Units:     1,
	}})
}

func bareMetalNodeClass(nodeSet *privatev1.ClusterNodeSet) string {
	if bmit := nodeSet.GetBaremetalInstanceType(); bmit != nil {
		return resourceClass("bmit", refKey(bmit))
	}
	if hostType := nodeSet.GetHostType(); hostType != nil {
		return resourceClass("host_type", refKey(hostType))
	}
	return ""
}

func bareMetalInstanceClass(ref *privatev1.BareMetalInstanceTypeLocalReference) string {
	if ref == nil {
		return ""
	}
	return resourceClass("bmit", refKey(ref))
}

func resourceClass(kind, value string) string {
	if value == "" {
		return ""
	}
	return kind + ":" + value
}
