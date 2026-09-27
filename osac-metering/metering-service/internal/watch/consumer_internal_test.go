/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package watch

import (
	"testing"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/osac-project/osac-metering/internal/projection"
)

func TestBuildComponentEventHandlesNilBaseData(t *testing.T) {
	c := &Consumer{}
	baseCE := cloudevents.NewEvent()
	baseCE.SetID("evt-1")
	baseCE.SetSource("osac-metering")
	baseCE.SetType("osac.resource.started.v1")
	// No SetData call: the base event carries zero-length data, so DataAs
	// leaves the target map nil without returning an error.

	ce, err := c.buildComponentEvent(&baseCE, "evt-1/node-a", map[string]any{"component": "worker"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var data map[string]any
	if err := ce.DataAs(&data); err != nil {
		t.Fatalf("unexpected error reading component event data: %v", err)
	}
	if data["billing_dimensions"] == nil {
		t.Errorf("expected billing_dimensions to be set on the component event even when the base event carried no data")
	}
}

func TestBuildStateContextUsesHeartbeatDeltaOrActiveStart(t *testing.T) {
	start := time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)
	lastHeartbeat := start.Add(45 * time.Minute)
	end := start.Add(time.Hour)
	consumer := &Consumer{}

	for _, test := range []struct {
		name            string
		lastHeartbeatAt *time.Time
		want            float64
	}{
		{name: "first close falls back to active start", want: 3600},
		{name: "later close reports only the tail", lastHeartbeatAt: &lastHeartbeat, want: 900},
	} {
		t.Run(test.name, func(t *testing.T) {
			state := &projection.ResourceState{
				IsBillable:      true,
				BillableSince:   &start,
				LastHeartbeatAt: test.lastHeartbeatAt,
			}
			got := consumer.buildStateContext(state, false, end, nil)
			if got.DurationSeconds == nil || *got.DurationSeconds != test.want {
				t.Fatalf("duration_seconds = %v, want %v", got.DurationSeconds, test.want)
			}
			if got.BillableSince == nil || !got.BillableSince.Equal(start) {
				t.Fatalf("billable_since = %v, want %v", got.BillableSince, start)
			}
		})
	}
}

func TestProjectionIsAheadTreatsTombstoneAsTerminal(t *testing.T) {
	existing := &projection.ResourceState{
		ResourceID:         "bmi-tombstoned",
		Deleted:            true,
		FulfillmentVersion: 4,
		CurrentState:       "RUNNING",
	}

	for _, version := range []int32{1, 4, 5, 100} {
		if !projectionIsAhead(existing, version, "RUNNING", nil, false) {
			t.Errorf("projectionIsAhead() = false for tombstone at incoming version %d", version)
		}
	}
}
