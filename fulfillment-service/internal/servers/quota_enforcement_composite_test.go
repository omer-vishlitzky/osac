/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License
is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
implied. See the License for the specific language governing permissions and limitations under the
License.
*/

package servers

import (
	"fmt"
	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/database"
	"github.com/osac-project/osac/fulfillment-service/internal/uuid"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

// The operator creates a cluster's hosts one at a time through the fulfillment API, with creator
// system, in the cluster's project, linked through status.cluster. The cluster's admission already
// counted the whole node set, so each host create converts one asked host into one existing host
// and must add nothing: an admitted cluster must be able to fulfill at a full tenant. The disk
// volumes of a VM follow the same rule, linked through their ci-<vm id>- name.
var _ = Describe("Quota enforcement of created children", func() {
	var instances *GenericServer[*privatev1.BareMetalInstance]
	var volumes *PrivateVolumesServer

	BeforeEach(func() {
		var err error
		instances, err = NewGenericServer[*privatev1.BareMetalInstance]().
			SetLogger(logger).
			SetService(privatev1.BareMetalInstances_ServiceDesc.ServiceName).
			SetAttributionLogic(attribution).
			SetTenancyLogic(tenancy).
			Build()
		Expect(err).ToNot(HaveOccurred())
		volumes, err = NewPrivateVolumesServer().
			SetLogger(logger).
			SetAttributionLogic(attribution).
			SetTenancyLogic(tenancy).
			SetTierResolver(quotaTierResolver).
			Build()
		Expect(err).ToNot(HaveOccurred())
	})

	insertRow := func(table, id, tenant, project, data string) {
		tx, err := database.TxFromContext(ctx)
		Expect(err).ToNot(HaveOccurred())
		_, err = tx.Exec(ctx,
			`insert into `+table+` (id, name, tenant, project, creator, data)
			values ($1, $1, $2, $3::ltree, 'system', $4::jsonb)`,
			id, tenant, project, data,
		)
		Expect(err).ToNot(HaveOccurred())
	}

	// A tenant override, so the seeded default of the type's dimension doesn't apply.
	setTenantLimit := func(dimension string, value int64) {
		tx, err := database.TxFromContext(ctx)
		Expect(err).ToNot(HaveOccurred())
		_, err = tx.Exec(ctx,
			`insert into quota_limits (id, name, tenant, project, creator, data)
			values ($1, $1, $2, ''::ltree, 'system', $3::jsonb)`,
			"limit-"+dimension, testTenant,
			`{"spec":{"dimension":"`+dimension+`","value":`+fmt.Sprint(value)+`}}`,
		)
		Expect(err).ToNot(HaveOccurred())
	}

	createHost := func(name string) error {
		var response *privatev1.BareMetalInstancesCreateResponse
		return instances.Create(ctx, privatev1.BareMetalInstancesCreateRequest_builder{
			Object: privatev1.BareMetalInstance_builder{
				Metadata: privatev1.Metadata_builder{Name: name, Tenant: testTenant}.Build(),
				Spec: privatev1.BareMetalInstanceSpec_builder{
					InstanceType: &privatev1.BareMetalInstanceTypeReference{Name: "metal-1tb"},
				}.Build(),
				Status: privatev1.BareMetalInstanceStatus_builder{
					Cluster: &privatev1.ClusterLocalReference{Id: "cluster-1"},
				}.Build(),
			}.Build(),
		}.Build(), &response)
	}

	It("lets the operator create a host of an admitted cluster at a full tenant", func() {
		// The tenant may hold exactly five hosts of the type.
		setTenantLimit("bare_metal.instances.metal-1tb", 5)
		// The type exists in the shared tenant.
		insertRow("bare_metal_instance_types", "metal-1tb", auth.SharedTenant, "", `{}`)
		// The cluster was admitted asking for five hosts of that type.
		insertRow("clusters", "cluster-1", testTenant, "",
			`{"spec":{"node_sets":{"workers":{"baremetal_instance_type":{"name":"metal-1tb"},"size":5}}}}`)

		// The operator creates the first host. The cluster's admission already counted it,
		// so the create adds nothing and must pass at the full tenant.
		Expect(createHost("host-1")).To(Succeed())
	})

	It("lets the operator create the last host of an admitted cluster at a full tenant", func() {
		setTenantLimit("bare_metal.instances.metal-1tb", 5)
		insertRow("bare_metal_instance_types", "metal-1tb", auth.SharedTenant, "", `{}`)
		insertRow("clusters", "cluster-1", testTenant, "",
			`{"spec":{"node_sets":{"workers":{"baremetal_instance_type":{"name":"metal-1tb"},"size":5}}}}`)

		// Four hosts already exist, linked to the cluster with creator system. Usage counts
		// the four rows plus the one host the cluster still asks for beyond them: five.
		for _, name := range []string{"host-1", "host-2", "host-3", "host-4"} {
			insertRow("bare_metal_instances", name, testTenant, "",
				`{"spec":{"instance_type":{"name":"metal-1tb"}},
				  "status":{"cluster":{"id":"cluster-1"}}}`)
		}

		// The fifth host converts the last asked host into an existing one: still five.
		Expect(createHost("host-5")).To(Succeed())
	})

	It("lets the operator create a VM's boot-disk volume for an admitted VM at a full tenant", func() {
		// The tenant may hold exactly one volume of ten GiB.
		setTenantLimit("storage.volumes", 1)
		setTenantLimit("storage.fast.gib", 10)
		// The VM was admitted asking for one ten-GiB fast boot disk. Nothing was created for
		// it yet, so usage holds exactly the ask: a full tenant.
		vmID := uuid.New()
		insertRow("instance_types", "it-small", auth.SharedTenant, "",
			`{"spec":{"vcpus":2,"memory_gib":4}}`)
		insertRow("compute_instances", vmID, testTenant, "",
			`{"spec":{"instance_type":{"name":"it-small"},
			   "boot_disk":{"size_gib":10,"storage_tier":{"name":"fast"}}}}`)

		// The operator creates the boot-disk volume, named after the VM the way the
		// controller names it. The VM's admission already counted it, so the create adds
		// nothing and must pass at the full tenant:
		_, err := volumes.Create(ctx, privatev1.VolumesCreateRequest_builder{
			Object: privatev1.Volume_builder{
				Metadata: privatev1.Metadata_builder{
					Name: "ci-" + vmID + "-boot", Tenant: testTenant,
				}.Build(),
				Spec: privatev1.VolumeSpec_builder{
					StorageTier: "fast",
					SizeGib:     10,
					AccessMode:  privatev1.VolumeAccessMode_VOLUME_ACCESS_MODE_READ_WRITE_ONCE,
				}.Build(),
			}.Build(),
		}.Build())
		Expect(err).To(Succeed())
	})
})
