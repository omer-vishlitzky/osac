/*
Copyright (c) 2025 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package reconciliation

import (
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/google/uuid"

	"github.com/osac-project/osac-metering/internal/events"
)

type CorrectionReason string

const (
	MissedCreation         CorrectionReason = "missed_creation"
	StateDrift             CorrectionReason = "state_drift"
	BillingDimensionsDrift CorrectionReason = "billing_dimensions_drift"
	MissedDeletion         CorrectionReason = "missed_deletion"
)

type AffectedInterval struct {
	From              time.Time `json:"from"`
	To                time.Time `json:"to"`
	OverbilledSeconds float64   `json:"overbilled_seconds"`
}

type correctionData struct {
	ResourceID              string            `json:"resource_id"`
	ResourceType            string            `json:"resource_type"`
	TenantID                string            `json:"tenant_id"`
	Reason                  CorrectionReason  `json:"reason"`
	CorrectedState          *string           `json:"corrected_state"`
	PreviousStateProjection *string           `json:"previous_state_in_projection"`
	ActualStateFromSource   *string           `json:"actual_state_from_source"`
	BillingDimensions       map[string]any    `json:"billing_dimensions,omitempty"`
	AffectedInterval        *AffectedInterval `json:"affected_interval,omitempty"`
	SchemaVersion           string            `json:"schema_version"`
}

func buildCorrectionEvent(
	resourceID, resourceType, tenantID string,
	reason CorrectionReason,
	projectionState, sourceState string,
	billingDimensions map[string]any,
	interval *AffectedInterval,
	now time.Time,
) cloudevents.Event {
	ce := cloudevents.NewEvent()
	ce.SetID(uuid.NewString())
	ce.SetSource("osac-metering/reconciler")
	ce.SetType("osac.resource.correction.v1")
	ce.SetTime(now)
	ce.SetExtension("osacresourceid", resourceID)
	ce.SetExtension("osacresourcetype", resourceType)
	ce.SetExtension("osactenant", tenantID)

	data := correctionData{
		ResourceID:              resourceID,
		ResourceType:            resourceType,
		TenantID:                tenantID,
		Reason:                  reason,
		CorrectedState:          events.NilIfEmpty(sourceState),
		PreviousStateProjection: events.NilIfEmpty(projectionState),
		ActualStateFromSource:   events.NilIfEmpty(sourceState),
		BillingDimensions:       billingDimensions,
		AffectedInterval:        interval,
		SchemaVersion:           "v1",
	}
	_ = ce.SetData(cloudevents.ApplicationJSON, data)

	return ce
}
