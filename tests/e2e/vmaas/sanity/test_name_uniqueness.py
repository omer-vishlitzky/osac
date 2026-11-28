from __future__ import annotations

import re
import subprocess
import uuid

import pytest

from tests.e2e.core.grpc_client import PRIVATE_API, PUBLIC_API, GRPCClient
from tests.e2e.core.helpers import assert_grpc_rejected, grpc_error_message, wait_for_virtual_network_deletion
from tests.e2e.core.k8s_client import K8sClient
from tests.e2e.core.runner import poll_until
from tests.e2e.core.wait_kinds import WaitResourceKind
from tests.e2e.vmaas.networking_lifecycle_helpers import create_and_wait_for_virtual_network

pytestmark = pytest.mark.sanity

DELETION_HOLD_FINALIZER = "osac-e2e-name-uniqueness-hold"


def _set_deletion_hold(private_grpc: GRPCClient, *, vn_id: str, present: bool) -> None:
    """Add or remove the finalizer that keeps a VirtualNetwork row from archiving.

    A deleted row with a pending finalizer stays in the database with
    ``metadata.deletionTimestamp`` set, so its name remains reserved for as
    long as the finalizer is there. Holding the row turns the deletion window
    the duplicate create probes into an observed state instead of a race
    against a teardown that can finish before the create is issued.
    """

    def update_finalizers() -> bool:
        try:
            object_ = private_grpc.call(service=f"{PRIVATE_API}.VirtualNetworks/Get", data={"id": vn_id})["object"]
        except subprocess.CalledProcessError as exc:
            combined = (exc.stderr or "") + "\n" + (exc.stdout or "")
            if re.search(r"(?m)^\s*Code:\s*NotFound\s*$", combined):
                # The row is already archived; there is nothing left to release.
                return not present
            raise
        metadata = object_["metadata"]
        finalizers = metadata.get("finalizers", [])
        if (DELETION_HOLD_FINALIZER in finalizers) == present:
            return True
        if present:
            finalizers = [*finalizers, DELETION_HOLD_FINALIZER]
        else:
            finalizers = [value for value in finalizers if value != DELETION_HOLD_FINALIZER]
        try:
            private_grpc.call(
                service=f"{PRIVATE_API}.VirtualNetworks/Update",
                data={
                    "object": {
                        "id": vn_id,
                        "metadata": {"finalizers": finalizers, "version": metadata.get("version", 0)},
                    },
                    "updateMask": {"paths": ["metadata.finalizers"]},
                    "lock": True,
                },
            )
        except subprocess.CalledProcessError as exc:
            combined = (exc.stderr or "") + "\n" + (exc.stdout or "")
            # The controllers write status concurrently; reread and retry.
            if re.search(r"(?m)^\s*Code:\s*Aborted\s*$", combined):
                return False
            if re.search(r"(?m)^\s*Code:\s*NotFound\s*$", combined):
                return not present
            raise
        return True

    poll_until(
        fn=update_finalizers,
        until=bool,
        retries=10,
        delay=1,
        description=f"VirtualNetwork {vn_id} deletion hold finalizer",
        resource_kind=WaitResourceKind.VIRTUAL_NETWORK,
        resource_id=vn_id,
    )


class TestVirtualNetworkProjectScopedUniqueness:
    """VirtualNetwork names must be unique within a (tenant, project) scope."""

    def test_duplicate_name_rejected(self, jwt_grpc_tenant1: GRPCClient) -> None:
        vn_name = f"dup-vn-{uuid.uuid4().hex[:8]}"
        vn_id: str | None = None
        try:
            vn_id = jwt_grpc_tenant1.create_virtual_network(name=vn_name, ipv4_cidr="172.16.0.0/16")
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                jwt_grpc_tenant1.create_virtual_network(name=vn_name, ipv4_cidr="172.17.0.0/16")
            assert_grpc_rejected(exc_info, "AlreadyExists")
            msg = grpc_error_message(exc_info.value)
            assert "virtual network" in msg.lower(), f"Error should mention 'virtual network', got: {msg}"
        finally:
            if vn_id:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id)

    def test_duplicate_name_during_deletion_rejected(
        self, jwt_grpc_tenant1: GRPCClient, k8s_hub_client: K8sClient, private_grpc: GRPCClient
    ) -> None:
        """A name stays reserved while the resource with it is being deleted.

        Intent: after a delete is accepted but before the resource is gone, a
        create reusing the name must be rejected with AlreadyExists. The delete
        window is held open deterministically by a finalizer on the API row;
        without it, teardown can archive the row before the duplicate create
        runs, which would leave the reservation unproven rather than enforced.
        """
        vn_name = f"del-dup-{uuid.uuid4().hex[:8]}"
        vn_id, cr_name = create_and_wait_for_virtual_network(jwt_grpc_tenant1, k8s_hub_client, vn_name, "172.23.0.0/16")
        duplicate_vn_id: str | None = None
        hold_applied = False
        delete_issued = False
        try:
            _set_deletion_hold(private_grpc, vn_id=vn_id, present=True)
            hold_applied = True
            jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id)
            delete_issued = True

            # Prove the deleted row still exists, so the create below is a
            # duplicate against a live reservation and not against nothing.
            deleting = private_grpc.call(service=f"{PRIVATE_API}.VirtualNetworks/Get", data={"id": vn_id})["object"]
            deleting_metadata = deleting.get("metadata", {})
            assert deleting_metadata.get("deletionTimestamp"), (
                f"VirtualNetwork {vn_name} has no deletionTimestamp after the delete was accepted"
            )
            assert DELETION_HOLD_FINALIZER in deleting_metadata.get("finalizers", []), (
                f"VirtualNetwork {vn_name} lost the deletion hold before the duplicate create"
            )

            try:
                duplicate_vn_id = jwt_grpc_tenant1.create_virtual_network(name=vn_name, ipv4_cidr="172.28.0.0/16")
            except subprocess.CalledProcessError as exc:
                combined = (exc.stderr or "") + (exc.stdout or "")
                assert re.search(r"Code:\s*AlreadyExists", combined), (
                    f"Expected AlreadyExists for a name still being deleted, got: {combined.strip()}"
                )
            else:
                pytest.fail(
                    f"VirtualNetworks/Create accepted {vn_name!r} while the original row still "
                    "existed with a deletion timestamp: names are no longer reserved during "
                    "deletion (regression)"
                )
        finally:
            # Release the hold FIRST: it must run even when the duplicate
            # cleanup below raises, or the held row can never archive and
            # both the row and the CR are left behind.
            if hold_applied or delete_issued:
                _set_deletion_hold(private_grpc, vn_id=vn_id, present=False)
            if duplicate_vn_id is not None:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=duplicate_vn_id)
            if not delete_issued:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id)
            wait_for_virtual_network_deletion(k8s=k8s_hub_client, name=cr_name)

    def test_same_name_different_tenants_succeeds(
        self, jwt_grpc_tenant1: GRPCClient, jwt_grpc_tenant2: GRPCClient
    ) -> None:
        vn_name = f"cross-t-{uuid.uuid4().hex[:8]}"
        vn_id_t1: str | None = None
        vn_id_t2: str | None = None
        try:
            vn_id_t1 = jwt_grpc_tenant1.create_virtual_network(name=vn_name, ipv4_cidr="172.29.0.0/16")
            vn_id_t2 = jwt_grpc_tenant2.create_virtual_network(name=vn_name, ipv4_cidr="172.31.0.0/16")
            assert vn_id_t1 != vn_id_t2
        finally:
            if vn_id_t1:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id_t1)
            if vn_id_t2:
                jwt_grpc_tenant2.delete_virtual_network(vn_id=vn_id_t2)

    @pytest.mark.skip(
        reason="Projects feature not yet implemented - projects must exist before resources can reference them"
    )
    def test_same_name_different_projects_succeeds(self, jwt_grpc_tenant1: GRPCClient) -> None:
        vn_name = f"cross-p-{uuid.uuid4().hex[:8]}"
        vn_id_p1: str | None = None
        vn_id_p2: str | None = None
        try:
            vn_id_p1 = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.VirtualNetworks/Create",
                data={
                    "object": {
                        "metadata": {"name": vn_name, "project": "project-alpha"},
                        "spec": {"ipv4_cidr": "172.24.0.0/16"},
                    }
                },
            )["object"]["id"]
            vn_id_p2 = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.VirtualNetworks/Create",
                data={
                    "object": {
                        "metadata": {"name": vn_name, "project": "project-beta"},
                        "spec": {"ipv4_cidr": "172.25.0.0/16"},
                    }
                },
            )["object"]["id"]
            assert vn_id_p1 != vn_id_p2
        finally:
            if vn_id_p1:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id_p1)
            if vn_id_p2:
                jwt_grpc_tenant1.delete_virtual_network(vn_id=vn_id_p2)


class TestRoleGlobalUniqueness:
    """Roles are globally unique: same name in different tenants is rejected."""

    @pytest.mark.skip(reason="JWT tokens lack permission to create Roles - requires admin credentials")
    def test_duplicate_role_name_different_tenants_rejected(
        self, jwt_grpc_tenant1: GRPCClient, jwt_grpc_tenant2: GRPCClient
    ) -> None:
        role_name = f"e2e-role-{uuid.uuid4().hex[:8]}"
        role_id: str | None = None
        try:
            response = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.Roles/Create", data={"object": {"metadata": {"name": role_name}}}
            )
            role_id = response["object"]["id"]

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                jwt_grpc_tenant2.call(
                    service=f"{PUBLIC_API}.Roles/Create", data={"object": {"metadata": {"name": role_name}}}
                )
            assert_grpc_rejected(exc_info, "AlreadyExists")
        finally:
            if role_id:
                jwt_grpc_tenant1.call(service=f"{PUBLIC_API}.Roles/Delete", data={"id": role_id})


class TestUserTenantScopedUniqueness:
    """User names are tenant-scoped: unique within a tenant, allowed across tenants."""

    @pytest.mark.skip(reason="JWT tokens lack permission to create Users - requires admin credentials")
    def test_duplicate_user_name_same_tenant_rejected(self, jwt_grpc_tenant1: GRPCClient) -> None:
        user_name = f"e2e-user-{uuid.uuid4().hex[:8]}"
        user_id: str | None = None
        try:
            response = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.Users/Create", data={"object": {"metadata": {"name": user_name}}}
            )
            user_id = response["object"]["id"]

            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                jwt_grpc_tenant1.call(
                    service=f"{PUBLIC_API}.Users/Create", data={"object": {"metadata": {"name": user_name}}}
                )
            assert_grpc_rejected(exc_info, "AlreadyExists")
        finally:
            if user_id:
                jwt_grpc_tenant1.call(service=f"{PUBLIC_API}.Users/Delete", data={"id": user_id})

    @pytest.mark.skip(reason="JWT tokens lack permission to create Users - requires admin credentials")
    def test_same_user_name_different_tenants_succeeds(
        self, jwt_grpc_tenant1: GRPCClient, jwt_grpc_tenant2: GRPCClient
    ) -> None:
        user_name = f"e2e-user-{uuid.uuid4().hex[:8]}"
        user_id_t1: str | None = None
        user_id_t2: str | None = None
        try:
            response_t1 = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.Users/Create", data={"object": {"metadata": {"name": user_name}}}
            )
            user_id_t1 = response_t1["object"]["id"]

            response_t2 = jwt_grpc_tenant2.call(
                service=f"{PUBLIC_API}.Users/Create", data={"object": {"metadata": {"name": user_name}}}
            )
            user_id_t2 = response_t2["object"]["id"]

            assert user_id_t1 != user_id_t2
        finally:
            if user_id_t1:
                jwt_grpc_tenant1.call(service=f"{PUBLIC_API}.Users/Delete", data={"id": user_id_t1})
            if user_id_t2:
                jwt_grpc_tenant2.call(service=f"{PUBLIC_API}.Users/Delete", data={"id": user_id_t2})
