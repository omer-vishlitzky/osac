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
	"errors"
	"fmt"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	clnt "sigs.k8s.io/controller-runtime/pkg/client"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	mcmanager "sigs.k8s.io/multicluster-runtime/pkg/manager"

	"github.com/osac-project/osac/osac-operator/api/v1alpha1"
	"github.com/osac-project/osac/osac-operator/internal/controller/feedback"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

var ErrExternalIPAttachmentNotFound = errors.New("external IP attachment not found in fulfillment service")

type ExternalIPAttachmentFeedbackReconciler struct {
	bridge              *feedback.Bridge[*v1alpha1.ExternalIPAttachment, *privatev1.ExternalIPAttachment]
	networkingNamespace string
}

func NewExternalIPAttachmentFeedbackReconciler(hubClient clnt.Client, grpcConn *grpc.ClientConn, networkingNamespace string) *ExternalIPAttachmentFeedbackReconciler {
	attachClient := privatev1.NewExternalIPAttachmentsClient(grpcConn)
	eipClient := privatev1.NewExternalIPsClient(grpcConn)
	r := &ExternalIPAttachmentFeedbackReconciler{networkingNamespace: networkingNamespace}
	r.bridge = &feedback.Bridge[*v1alpha1.ExternalIPAttachment, *privatev1.ExternalIPAttachment]{
		Client:    hubClient,
		Finalizer: osacExternalIPAttachmentFeedbackFinalizer,
		IDLabel:   osacExternalIPAttachmentIDLabel,
		Kind:      "ExternalIPAttachment",
		IDKey:     "attachmentID",
		NewObject: func() *v1alpha1.ExternalIPAttachment { return &v1alpha1.ExternalIPAttachment{} },
		Fetch: func(ctx context.Context, id string) (*privatev1.ExternalIPAttachment, error) {
			response, err := attachClient.Get(ctx, privatev1.ExternalIPAttachmentsGetRequest_builder{Id: id}.Build())
			if err != nil {
				if status.Code(err) == codes.NotFound {
					return nil, fmt.Errorf("%w: %w", ErrExternalIPAttachmentNotFound, err)
				}
				return nil, err
			}
			obj := response.GetObject()
			if obj == nil {
				return nil, fmt.Errorf("%w: response contained nil object", ErrExternalIPAttachmentNotFound)
			}
			if !obj.HasSpec() {
				obj.SetSpec(&privatev1.ExternalIPAttachmentSpec{})
			}
			if !obj.HasStatus() {
				obj.SetStatus(&privatev1.ExternalIPAttachmentStatus{})
			}
			return obj, nil
		},
		Save: func(ctx context.Context, remote *privatev1.ExternalIPAttachment) error {
			_, err := attachClient.Update(ctx, privatev1.ExternalIPAttachmentsUpdateRequest_builder{
				Object: remote,
			}.Build())
			return err
		},
		Signal: func(ctx context.Context, id string) error {
			_, err := attachClient.Signal(ctx, privatev1.ExternalIPAttachmentsSignalRequest_builder{
				Id: id,
			}.Build())
			return err
		},
		SyncUpdate:       newExternalIPAttachmentSyncUpdate(eipClient, hubClient),
		SyncDelete:       syncExternalIPAttachmentDelete,
		PostSaveOnDelete: newExternalIPAttachmentPostSaveOnDelete(eipClient, hubClient),
		IsNotFound:       func(err error) bool { return errors.Is(err, ErrExternalIPAttachmentNotFound) },
	}
	return r
}

func (r *ExternalIPAttachmentFeedbackReconciler) SetupWithManager(mgr mcmanager.Manager) error {
	localMgr := mgr.GetLocalManager()
	if localMgr == nil {
		return fmt.Errorf("local manager is nil")
	}

	return ctrl.NewControllerManagedBy(localMgr).
		Named("externalipattachment-feedback").
		For(&v1alpha1.ExternalIPAttachment{}, builder.WithPredicates(NetworkingNamespacePredicate(r.networkingNamespace))).
		Complete(r)
}

// Reconcile delegates to the shared feedback Bridge.
func (r *ExternalIPAttachmentFeedbackReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	return r.bridge.Reconcile(ctx, request)
}

// newExternalIPAttachmentSyncUpdate returns a SyncUpdate that captures eipClient
// for setting attached=true on the parent ExternalIP when Ready, and for syncing
// the parent's address to the attachment.
func newExternalIPAttachmentSyncUpdate(eipClient privatev1.ExternalIPsClient, hubClient clnt.Client) func(context.Context, *v1alpha1.ExternalIPAttachment, *privatev1.ExternalIPAttachment) error {
	return func(ctx context.Context, obj *v1alpha1.ExternalIPAttachment, remote *privatev1.ExternalIPAttachment) error {
		syncExternalIPAttachmentState(ctx, obj, remote)
		syncExternalIPAttachmentAddress(ctx, eipClient, remote)

		if obj.Status.Phase == v1alpha1.ExternalIPAttachmentPhaseReady {
			if err := syncAttachedOnParentExternalIP(ctx, eipClient, hubClient, obj.Namespace, remote, true); err != nil {
				ctrllog.FromContext(ctx).Error(err, "Failed to set attached on parent ExternalIP, will retry")
				return err
			}
		}
		return nil
	}
}

func syncExternalIPAttachmentDelete(_ context.Context, obj *v1alpha1.ExternalIPAttachment, remote *privatev1.ExternalIPAttachment) error {
	syncExternalIPAttachmentStateTransitionTime(obj, remote)
	if obj.Status.Phase == v1alpha1.ExternalIPAttachmentPhaseFailed {
		remote.GetStatus().SetState(privatev1.ExternalIPAttachmentState_EXTERNAL_IP_ATTACHMENT_STATE_FAILED)
		return nil
	}
	remote.GetStatus().SetState(privatev1.ExternalIPAttachmentState_EXTERNAL_IP_ATTACHMENT_STATE_DELETING)
	return nil
}

func syncExternalIPAttachmentStateTransitionTime(obj *v1alpha1.ExternalIPAttachment, remote *privatev1.ExternalIPAttachment) {
	if obj.Status.StateTransitionTime == nil {
		remote.GetStatus().ClearStateTransitionTime()
		return
	}
	remote.GetStatus().SetStateTransitionTime(timestamppb.New(obj.Status.StateTransitionTime.Time))
}

// newExternalIPAttachmentPostSaveOnDelete returns a PostSaveOnDelete that clears
// the attached flag on the parent ExternalIP after the attachment's DELETING
// state is persisted.
func newExternalIPAttachmentPostSaveOnDelete(eipClient privatev1.ExternalIPsClient, hubClient clnt.Client) func(context.Context, *v1alpha1.ExternalIPAttachment, *privatev1.ExternalIPAttachment) error {
	return func(ctx context.Context, obj *v1alpha1.ExternalIPAttachment, remote *privatev1.ExternalIPAttachment) error {
		if err := syncAttachedOnParentExternalIP(ctx, eipClient, hubClient, obj.Namespace, remote, false); err != nil {
			ctrllog.FromContext(ctx).Error(err, "Failed to clear attached on parent ExternalIP, will retry")
			return err
		}
		return nil
	}
}

func syncExternalIPAttachmentState(ctx context.Context, obj *v1alpha1.ExternalIPAttachment, remote *privatev1.ExternalIPAttachment) {
	switch obj.Status.Phase {
	case v1alpha1.ExternalIPAttachmentPhaseProgressing:
		remote.GetStatus().SetState(privatev1.ExternalIPAttachmentState_EXTERNAL_IP_ATTACHMENT_STATE_PENDING)
	case v1alpha1.ExternalIPAttachmentPhaseReady:
		remote.GetStatus().SetState(privatev1.ExternalIPAttachmentState_EXTERNAL_IP_ATTACHMENT_STATE_READY)
	case v1alpha1.ExternalIPAttachmentPhaseFailed:
		remote.GetStatus().SetState(privatev1.ExternalIPAttachmentState_EXTERNAL_IP_ATTACHMENT_STATE_FAILED)
	default:
		log := ctrllog.FromContext(ctx)
		log.Info("Unknown phase, will ignore it", "phase", obj.Status.Phase)
	}

	syncExternalIPAttachmentStateTransitionTime(obj, remote)
}

func syncExternalIPAttachmentAddress(ctx context.Context, eipClient privatev1.ExternalIPsClient, remote *privatev1.ExternalIPAttachment) {
	externalIPRef := remote.GetSpec().GetExternalIp()
	if externalIPRef.GetId() == "" {
		return
	}
	response, err := eipClient.Get(ctx, privatev1.ExternalIPsGetRequest_builder{
		Id: externalIPRef.GetId(),
	}.Build())
	if err != nil {
		ctrllog.FromContext(ctx).Error(err, "Failed to fetch parent ExternalIP for address sync", "externalIPID", externalIPRef.GetId())
		return
	}
	obj := response.GetObject()
	if obj == nil || !obj.HasStatus() {
		return
	}
	if addr := obj.GetStatus().GetAddress(); addr != "" {
		remote.GetStatus().SetExternalIpAddress(addr)
	}
}

func syncAttachedOnParentExternalIP(ctx context.Context, eipClient privatev1.ExternalIPsClient, hubClient clnt.Client, namespace string, remote *privatev1.ExternalIPAttachment, attached bool) error {
	attribution, err := externalIPAttribution(remote, attached)
	if err != nil {
		return err
	}
	externalIPRef := remote.GetSpec().GetExternalIp()
	if externalIPRef.GetId() == "" {
		if attached {
			return fmt.Errorf("external IP attachment %s has no external IP identifier", remote.GetId())
		}
		return nil
	}

	response, err := eipClient.Get(ctx, privatev1.ExternalIPsGetRequest_builder{
		Id: externalIPRef.GetId(),
	}.Build())
	if err != nil {
		if status.Code(err) == codes.NotFound {
			ctrllog.FromContext(ctx).Info("Parent ExternalIP not found, skipping attached sync", "externalIPID", externalIPRef.GetId())
			return nil
		}
		return err
	}

	externalIP := response.GetObject()
	if externalIP == nil {
		return fmt.Errorf("parent ExternalIP %s: response contained nil object", externalIPRef.GetId())
	}
	if !externalIP.HasStatus() {
		externalIP.SetStatus(&privatev1.ExternalIPStatus{})
	}

	var attachmentTransitionTime *timestamppb.Timestamp
	if value := remote.GetStatus().GetStateTransitionTime(); value != nil {
		attachmentTransitionTime = proto.Clone(value).(*timestamppb.Timestamp)
	}
	if externalIP.GetStatus().GetAttached() == attached &&
		proto.Equal(externalIP.GetStatus().GetAttribution(), attribution) &&
		proto.Equal(externalIP.GetStatus().GetAttachmentTransitionTime(), attachmentTransitionTime) {
		return syncExternalIPCRDTransitionTime(ctx, hubClient, namespace, externalIPRef.GetId(), attachmentTransitionTime)
	}

	externalIP.GetStatus().SetAttached(attached)
	externalIP.GetStatus().SetAttribution(attribution)
	externalIP.GetStatus().SetAttachmentTransitionTime(attachmentTransitionTime)
	_, err = eipClient.Update(ctx, privatev1.ExternalIPsUpdateRequest_builder{
		Object: externalIP,
	}.Build())
	if err != nil {
		return err
	}

	return syncExternalIPCRDTransitionTime(ctx, hubClient, namespace, externalIPRef.GetId(), attachmentTransitionTime)
}

func externalIPAttribution(remote *privatev1.ExternalIPAttachment, attached bool) (*privatev1.ExternalIPAttribution, error) {
	if !attached {
		return nil, nil
	}
	spec := remote.GetSpec()
	switch {
	case spec.HasComputeInstance():
		if spec.GetComputeInstance().GetId() == "" {
			return nil, fmt.Errorf("external IP attachment %s has an empty compute instance attribution ID", remote.GetId())
		}
		if spec.GetTargetEndpoint() != privatev1.ExternalIPAttachmentEndpoint_EXTERNAL_IP_ATTACHMENT_ENDPOINT_UNSPECIFIED {
			return nil, fmt.Errorf("external IP attachment %s has an endpoint for a compute instance target", remote.GetId())
		}
		return privatev1.ExternalIPAttribution_builder{
			ComputeInstance: proto.Clone(spec.GetComputeInstance()).(*privatev1.ComputeInstanceLocalReference),
		}.Build(), nil
	case spec.HasCluster():
		if spec.GetCluster().GetId() == "" {
			return nil, fmt.Errorf("external IP attachment %s has an empty cluster attribution ID", remote.GetId())
		}
		endpoint := spec.GetTargetEndpoint()
		if endpoint != privatev1.ExternalIPAttachmentEndpoint_EXTERNAL_IP_ATTACHMENT_ENDPOINT_API &&
			endpoint != privatev1.ExternalIPAttachmentEndpoint_EXTERNAL_IP_ATTACHMENT_ENDPOINT_INGRESS {
			return nil, fmt.Errorf("external IP attachment %s has an invalid cluster endpoint %s", remote.GetId(), endpoint)
		}
		return privatev1.ExternalIPAttribution_builder{
			Cluster:  proto.Clone(spec.GetCluster()).(*privatev1.ClusterLocalReference),
			Endpoint: endpoint,
		}.Build(), nil
	case spec.HasBaremetalInstance():
		if spec.GetBaremetalInstance().GetId() == "" {
			return nil, fmt.Errorf("external IP attachment %s has an empty bare metal instance attribution ID", remote.GetId())
		}
		if spec.GetTargetEndpoint() != privatev1.ExternalIPAttachmentEndpoint_EXTERNAL_IP_ATTACHMENT_ENDPOINT_UNSPECIFIED {
			return nil, fmt.Errorf("external IP attachment %s has an endpoint for a bare metal instance target", remote.GetId())
		}
		return privatev1.ExternalIPAttribution_builder{
			BaremetalInstance: proto.Clone(spec.GetBaremetalInstance()).(*privatev1.BareMetalInstanceLocalReference),
		}.Build(), nil
	default:
		return nil, fmt.Errorf("external IP attachment %s has no attribution target", remote.GetId())
	}
}

func syncExternalIPCRDTransitionTime(ctx context.Context, hubClient clnt.Client, namespace, externalIPID string, transitionTime *timestamppb.Timestamp) error {
	if hubClient == nil {
		return nil
	}
	list := &v1alpha1.ExternalIPList{}
	if err := hubClient.List(ctx, list, clnt.InNamespace(namespace), clnt.MatchingLabels{osacExternalIPIDLabel: externalIPID}); err != nil {
		return err
	}
	if len(list.Items) == 0 {
		return fmt.Errorf("ExternalIP CR for ID %s was not found in namespace %s", externalIPID, namespace)
	}
	if len(list.Items) != 1 {
		return fmt.Errorf("expected one ExternalIP CR for ID %s, found %d", externalIPID, len(list.Items))
	}
	parent := &list.Items[0]
	if transitionTime == nil {
		parent.Status.AttachmentTransitionTime = nil
	} else {
		time := metav1.NewTime(transitionTime.AsTime())
		parent.Status.AttachmentTransitionTime = &time
	}
	return hubClient.Status().Update(ctx, parent)
}
