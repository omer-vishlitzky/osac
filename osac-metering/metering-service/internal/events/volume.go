package events

import (
	"fmt"
	"strings"
	"time"

	"github.com/osac-project/osac-metering/schema"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

const VolumeStatePrefix = "VOLUME_STATE_"

const (
	VolumeStateUnspecified = "UNSPECIFIED"
	VolumeStateCreating    = "CREATING"
	VolumeStateAvailable   = "AVAILABLE"
	VolumeStateFailed      = "FAILED"
	VolumeStateDeleting    = "DELETING"
	VolumeStateDeleted     = "DELETED"
)

// Volume transitions are intentionally exhaustive for every state that may be
// observed after creation. Missing entries are invalid transitions.
var volumeTransitions = TransitionTable{
	{StateEmpty, VolumeStateUnspecified}: {Skip: true},
	{StateEmpty, VolumeStateCreating}:    {Skip: true},
	{StateEmpty, VolumeStateAvailable}:   {EventType: eventBillableStart},
	{StateEmpty, VolumeStateFailed}:      {Skip: true},
	{StateEmpty, VolumeStateDeleting}:    {Skip: true},
	{StateEmpty, VolumeStateDeleted}:     {Skip: true},

	{VolumeStateUnspecified, VolumeStateUnspecified}: {Skip: true},
	{VolumeStateUnspecified, VolumeStateCreating}:    {Skip: true},
	{VolumeStateUnspecified, VolumeStateAvailable}:   {EventType: eventBillableStart},
	{VolumeStateUnspecified, VolumeStateFailed}:      {Skip: true},
	{VolumeStateUnspecified, VolumeStateDeleting}:    {Skip: true},
	{VolumeStateUnspecified, VolumeStateDeleted}:     {Skip: true},

	{VolumeStateCreating, VolumeStateUnspecified}: {Skip: true},
	{VolumeStateCreating, VolumeStateCreating}:    {Skip: true},
	{VolumeStateCreating, VolumeStateAvailable}:   {EventType: eventBillableStart},
	{VolumeStateCreating, VolumeStateFailed}:      {Skip: true},
	{VolumeStateCreating, VolumeStateDeleting}:    {Skip: true},
	{VolumeStateCreating, VolumeStateDeleted}:     {Skip: true},

	{VolumeStateAvailable, VolumeStateAvailable}: {Skip: true},
	{VolumeStateAvailable, VolumeStateFailed}:    {EventType: EventSuspended},
	{VolumeStateAvailable, VolumeStateDeleting}:  {EventType: EventSuspended},
	{VolumeStateAvailable, VolumeStateDeleted}:   {EventType: EventSuspended},

	{VolumeStateFailed, VolumeStateFailed}:   {Skip: true},
	{VolumeStateFailed, VolumeStateDeleting}: {Skip: true},
	{VolumeStateFailed, VolumeStateDeleted}:  {Skip: true},

	{VolumeStateDeleting, VolumeStateDeleting}: {Skip: true},
	{VolumeStateDeleting, VolumeStateDeleted}:  {Skip: true},

	{VolumeStateDeleted, VolumeStateDeleted}: {Skip: true},
}

type volumeMapper struct {
	volume *privatev1.Volume
}

func (m *volumeMapper) ResourceType() string { return schema.ResourceTypeVolume }
func (m *volumeMapper) ResourceID() string   { return m.volume.GetId() }

func (m *volumeMapper) TenantID() string {
	return m.volume.GetMetadata().GetTenant()
}

func (m *volumeMapper) ProjectID() *string {
	return NilIfEmpty(m.volume.GetMetadata().GetProject())
}

func (m *volumeMapper) CatalogItemID() *string { return nil }
func (m *volumeMapper) TemplateID() *string    { return nil }

func (m *volumeMapper) CurrentState() string {
	return VolumeCurrentState(m.volume)
}

func VolumeCurrentState(volume *privatev1.Volume) string {
	if volume.GetMetadata().GetDeletionTimestamp() != nil {
		return VolumeStateDeleting
	}
	state := volume.GetStatus().GetState()
	return strings.TrimPrefix(state.String(), VolumeStatePrefix)
}

func (m *volumeMapper) FulfillmentVersion() int32 {
	return m.volume.GetMetadata().GetVersion()
}

func (m *volumeMapper) IsBillable() bool {
	return m.CurrentState() == VolumeStateAvailable && volumeHasBlockIdentity(m.volume)
}

func volumeHasBlockIdentity(volume *privatev1.Volume) bool {
	return volume.GetStatus().GetProtocol() == privatev1.StorageProtocol_STORAGE_PROTOCOL_BLOCK &&
		volume.GetStatus().GetVendorVolumeId() != ""
}

func (m *volumeMapper) BillingDimensionsMap() (map[string]any, error) {
	return VolumeBillingDimensions(m.volume)
}

func VolumeBillingDimensions(volume *privatev1.Volume) (map[string]any, error) {
	dimensions := map[string]any{
		"volume_id":    volume.GetId(),
		"tenant_id":    volume.GetMetadata().GetTenant(),
		"project_id":   volume.GetMetadata().GetProject(),
		"storage_tier": volume.GetSpec().GetStorageTier(),
		"size_gib":     volume.GetSpec().GetSizeGib(),
	}
	return dimensions, ValidateBillingDimensions(schema.ResourceTypeVolume, dimensions)
}

func IsVolumeBillableState(state string) bool { return state == VolumeStateAvailable }

func IsVolumeBillable(volume *privatev1.Volume) bool {
	return VolumeCurrentState(volume) == VolumeStateAvailable && volumeHasBlockIdentity(volume)
}

func IsVolumeTransientState(string) bool { return false }

func (m *volumeMapper) CloudEventType(eventType privatev1.EventType, previousState string) (string, error) {
	if eventType == privatev1.EventType_EVENT_TYPE_OBJECT_CREATED && m.CurrentState() != VolumeStateCreating {
		return "", fmt.Errorf("volume OBJECT_CREATED must be CREATING, got %s", m.CurrentState())
	}
	if eventType == privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED &&
		previousState == VolumeStateAvailable && m.CurrentState() == VolumeStateAvailable && m.IsBillable() {
		return eventBillableStart, nil
	}

	result, err := ResolveCloudEventType(volumeTransitions, eventType, previousState, m.CurrentState())
	if err != nil {
		return "", err
	}
	if eventType == privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED &&
		(result == eventBillableStart || result == EventSuspended) &&
		!volumeHasBlockIdentity(m.volume) && m.CurrentState() == VolumeStateAvailable {
		return "", ErrSkipTransition
	}
	if eventType == privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED &&
		result == EventSuspended && previousState == VolumeStateAvailable &&
		!volumeHasBlockIdentity(m.volume) {
		return "", ErrSkipTransition
	}
	return result, nil
}

func (m *volumeMapper) TransitionTime(event *privatev1.Event, _ string) (time.Time, error) {
	return VolumeTransitionTime(m.volume, event)
}

func VolumeTransitionTime(volume *privatev1.Volume, event *privatev1.Event) (time.Time, error) {
	if event.GetType() == privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED && volume.GetMetadata().GetDeletionTimestamp() != nil {
		return volume.GetMetadata().GetDeletionTimestamp().AsTime(), nil
	}
	return ResolveTransitionTime(
		event.GetType(),
		event.GetTimestamp(),
		volume.GetMetadata().GetCreationTimestamp(),
		volume.GetStatus().GetStateTransitionTime(),
		volume.GetId(),
	)
}
