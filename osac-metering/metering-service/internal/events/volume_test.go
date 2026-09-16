package events_test

import (
	"errors"
	"testing"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/osac-project/osac-metering/internal/events"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

func TestVolumeWatchLifecycle(t *testing.T) {
	creation := time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)
	available := creation.Add(time.Minute)
	deletion := creation.Add(time.Hour)
	volume := &privatev1.Volume{
		Id: "volume-1",
		Metadata: &privatev1.Metadata{
			Tenant:            "tenant-1",
			Project:           "project-1",
			CreationTimestamp: timestamppb.New(creation),
		},
		Spec: &privatev1.VolumeSpec{StorageTier: "gold", SizeGib: 100},
		Status: &privatev1.VolumeStatus{
			State:               privatev1.VolumeState_VOLUME_STATE_CREATING,
			Protocol:            privatev1.StorageProtocol_STORAGE_PROTOCOL_BLOCK,
			StateTransitionTime: timestamppb.New(creation),
		},
	}

	created := mapVolumeEvent(t, &privatev1.Event{
		Id:      "volume-created",
		Type:    privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
		Payload: &privatev1.Event_Volume{Volume: volume},
	})
	if created.Type() != events.EventCreated {
		t.Fatalf("created event type = %q", created.Type())
	}

	volume.Status.State = privatev1.VolumeState_VOLUME_STATE_AVAILABLE
	volume.Status.VendorVolumeId = "vendor-1"
	volume.Status.StateTransitionTime = timestamppb.New(available)
	started := mapVolumeEvent(t, &privatev1.Event{
		Id:      "volume-available",
		Type:    privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED,
		Payload: &privatev1.Event_Volume{Volume: volume},
	})
	if started.Type() != events.EventStarted || !started.Time().Equal(available) {
		t.Fatalf("started event = type %q time %s", started.Type(), started.Time())
	}

	volume.Metadata.DeletionTimestamp = timestamppb.New(deletion)
	volume.Status.State = privatev1.VolumeState_VOLUME_STATE_DELETING
	suspended := mapVolumeEvent(t, &privatev1.Event{
		Id:      "volume-deleting",
		Type:    privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED,
		Payload: &privatev1.Event_Volume{Volume: volume},
	})
	if suspended.Type() != events.EventSuspended || !suspended.Time().Equal(deletion) {
		t.Fatalf("suspended event = type %q time %s", suspended.Type(), suspended.Time())
	}
}

func TestVolumeDoesNotBillWithoutBlockIdentity(t *testing.T) {
	volume := &privatev1.Volume{
		Id:       "volume-no-id",
		Metadata: &privatev1.Metadata{Tenant: "tenant-1"},
		Spec:     &privatev1.VolumeSpec{StorageTier: "gold", SizeGib: 1},
		Status: &privatev1.VolumeStatus{
			State:               privatev1.VolumeState_VOLUME_STATE_AVAILABLE,
			Protocol:            privatev1.StorageProtocol_STORAGE_PROTOCOL_NFS,
			StateTransitionTime: timestamppb.Now(),
		},
	}
	mapper, err := events.MapperForEvent(&privatev1.Event{Payload: &privatev1.Event_Volume{Volume: volume}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = mapper.CloudEventType(privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED, events.VolumeStateCreating)
	if !errors.Is(err, events.ErrSkipTransition) {
		t.Fatalf("expected non-billable volume to skip, got %v", err)
	}
}

func mapVolumeEvent(t *testing.T, event *privatev1.Event) *cloudevents.Event {
	t.Helper()
	mapper, err := events.MapperForEvent(event)
	if err != nil {
		t.Fatal(err)
	}
	dims, err := mapper.BillingDimensionsMap()
	if err != nil {
		t.Fatal(err)
	}
	previous := ""
	if event.GetType() == privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED {
		previous = events.VolumeStateCreating
		if event.GetId() == "volume-deleting" {
			previous = events.VolumeStateAvailable
		}
	}
	ce, err := events.MapWatchEvent(event, mapper, &events.StateContext{PreviousState: previous}, dims)
	if err != nil {
		t.Fatal(err)
	}
	return ce
}
