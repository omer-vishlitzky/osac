/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package controller

import (
	"context"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/osac-project/osac/osac-operator/api/v1alpha1"
	privatev1 "github.com/osac-project/osac/osac-operator/internal/api/osac/private/v1"
)

func TestDeleteFeedbackCopiesAuthoritativeTransitionTimes(t *testing.T) {
	want := time.Date(2026, time.January, 2, 3, 4, 5, 0, time.UTC)

	t.Run("ExternalIP", func(t *testing.T) {
		obj := &v1alpha1.ExternalIP{Status: v1alpha1.ExternalIPStatus{
			State:               v1alpha1.ExternalIPStateAllocated,
			StateTransitionTime: &metav1.Time{Time: want},
		}}
		remote := &privatev1.ExternalIP{Status: &privatev1.ExternalIPStatus{
			StateTransitionTime: timestamppb.New(want.Add(-time.Hour)),
		}}

		if err := syncExternalIPDelete(context.Background(), obj, remote); err != nil {
			t.Fatal(err)
		}
		assertTransitionTime(t, remote.GetStatus().GetStateTransitionTime(), want)
	})

	t.Run("ExternalIPAttachment", func(t *testing.T) {
		obj := &v1alpha1.ExternalIPAttachment{Status: v1alpha1.ExternalIPAttachmentStatus{
			Phase:               v1alpha1.ExternalIPAttachmentPhaseReady,
			StateTransitionTime: &metav1.Time{Time: want},
		}}
		remote := &privatev1.ExternalIPAttachment{Status: &privatev1.ExternalIPAttachmentStatus{
			StateTransitionTime: timestamppb.New(want.Add(-time.Hour)),
		}}

		if err := syncExternalIPAttachmentDelete(context.Background(), obj, remote); err != nil {
			t.Fatal(err)
		}
		assertTransitionTime(t, remote.GetStatus().GetStateTransitionTime(), want)
	})

	t.Run("NATGateway", func(t *testing.T) {
		obj := &v1alpha1.NATGateway{Status: v1alpha1.NATGatewayStatus{
			Phase:               v1alpha1.NATGatewayPhaseReady,
			StateTransitionTime: &metav1.Time{Time: want},
		}}
		remote := &privatev1.NATGateway{Status: &privatev1.NATGatewayStatus{
			StateTransitionTime: timestamppb.New(want.Add(-time.Hour)),
		}}

		if err := syncNATGatewayDelete(context.Background(), obj, remote); err != nil {
			t.Fatal(err)
		}
		assertTransitionTime(t, remote.GetStatus().GetStateTransitionTime(), want)
	})
}

func TestExternalIPDeleteRecordsTransitionTime(t *testing.T) {
	status := &v1alpha1.ExternalIPStatus{}

	setExternalIPDeleting(status)
	first := status.StateTransitionTime
	if first == nil {
		t.Fatal("deletion transition time was not recorded")
	}

	setExternalIPDeleting(status)
	if !status.StateTransitionTime.Time.Equal(first.Time) {
		t.Fatal("deletion transition time changed on repeated reconciliation")
	}
}

func TestSyncExternalIPUpdateCopiesAttachmentTransitionTime(t *testing.T) {
	want := time.Date(2026, time.January, 2, 3, 4, 5, 0, time.UTC)
	obj := &v1alpha1.ExternalIP{Status: v1alpha1.ExternalIPStatus{
		AttachmentTransitionTime: &metav1.Time{Time: want},
	}}
	remote := &privatev1.ExternalIP{Status: &privatev1.ExternalIPStatus{}}

	if err := syncExternalIPUpdate(context.Background(), obj, remote); err != nil {
		t.Fatal(err)
	}
	assertTransitionTime(t, remote.GetStatus().GetAttachmentTransitionTime(), want)
}

func TestExternalIPAttributionRejectsInvalidTargetAndEndpoint(t *testing.T) {
	tests := map[string]*privatev1.ExternalIPAttachment{
		"empty compute ID": {
			Id:   "attachment",
			Spec: &privatev1.ExternalIPAttachmentSpec{},
		},
		"cluster endpoint unspecified": {
			Id:   "attachment",
			Spec: &privatev1.ExternalIPAttachmentSpec{},
		},
		"compute endpoint set": {
			Id:   "attachment",
			Spec: &privatev1.ExternalIPAttachmentSpec{},
		},
	}
	tests["empty compute ID"].GetSpec().SetComputeInstance(&privatev1.ComputeInstanceLocalReference{})
	tests["cluster endpoint unspecified"].GetSpec().SetCluster(&privatev1.ClusterLocalReference{Id: "cluster"})
	tests["compute endpoint set"].GetSpec().SetComputeInstance(&privatev1.ComputeInstanceLocalReference{Id: "compute"})
	tests["compute endpoint set"].GetSpec().SetTargetEndpoint(privatev1.ExternalIPAttachmentEndpoint_EXTERNAL_IP_ATTACHMENT_ENDPOINT_API)

	for name, attachment := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := externalIPAttribution(attachment, true); err == nil {
				t.Fatal("expected invalid attribution to fail")
			}
		})
	}
}

func TestSyncExternalIPCRDTransitionTimeRequiresParent(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	hubClient := fake.NewClientBuilder().WithScheme(scheme).Build()

	err := syncExternalIPCRDTransitionTime(context.Background(), hubClient, "networking", "external-ip-id", nil)
	if err == nil {
		t.Fatal("expected missing parent ExternalIP CR to fail")
	}
}

func assertTransitionTime(t *testing.T, got *timestamppb.Timestamp, want time.Time) {
	t.Helper()
	if got == nil {
		t.Fatal("transition time was not propagated")
	}
	if !got.AsTime().Equal(want) {
		t.Fatalf("transition time = %s, want %s", got.AsTime(), want)
	}
}
