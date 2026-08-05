/*
Copyright (c) 2025 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package heartbeat

import (
	"context"
	"fmt"
	"time"

	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-logr/logr"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"

	"github.com/osac-project/osac-metering/internal/events"
	kafkapub "github.com/osac-project/osac-metering/internal/kafka"
	"github.com/osac-project/osac-metering/internal/projection"
)

var (
	heartbeatLag = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "osac_metering_heartbeat_lag_seconds",
		Help: "Max staleness of heartbeats per resource type",
	}, []string{"resource_type"})

	projectionResources = promauto.NewGaugeVec(prometheus.GaugeOpts{
		Name: "osac_metering_state_projection_resources",
		Help: "Number of resources in State Projection",
	}, []string{"resource_type", "is_billable"})
)

type Generator struct {
	store     projection.Store
	publisher kafkapub.EventPublisher
	logger    logr.Logger
	interval  time.Duration
}

func NewGenerator(
	store projection.Store,
	publisher kafkapub.EventPublisher,
	logger logr.Logger,
	interval time.Duration,
) *Generator {
	return &Generator{
		store:     store,
		publisher: publisher,
		logger:    logger,
		interval:  interval,
	}
}

func (g *Generator) Run(ctx context.Context) error {
	ticker := time.NewTicker(g.interval)
	defer ticker.Stop()

	g.logger.Info("heartbeat generator started", "interval", g.interval)

	for {
		select {
		case <-ctx.Done():
			g.logger.Info("heartbeat generator stopping")
			return nil
		case <-ticker.C:
			if err := g.tick(ctx); err != nil {
				g.logger.Error(err, "heartbeat tick failed")
			}
		}
	}
}

func (g *Generator) tick(ctx context.Context) error {
	billable, err := g.store.ListBillable(ctx)
	if err != nil {
		return fmt.Errorf("querying billable resources: %w", err)
	}

	g.updateGauges(billable)

	if len(billable) == 0 {
		return nil
	}

	now := time.Now().UTC()
	var publishedIDs []string

	// TODO: All-or-nothing is a scale concern past ~1K billable resources. At 10K VMs,
	// one Kafka failure after 9999 publishes discards all progress. Revisit with
	// batch checkpointing when we hit scale.
	for i := range billable {
		ce := g.buildHeartbeatEvent(&billable[i], now)
		if err := g.publisher.Publish(ctx, ce); err != nil {
			if len(publishedIDs) > 0 {
				if cpErr := g.store.UpdateLastHeartbeat(ctx, publishedIDs, now); cpErr != nil {
					g.logger.Error(cpErr, "failed to checkpoint partial heartbeat progress",
						"published", len(publishedIDs))
				}
			}
			return fmt.Errorf("publishing heartbeat for %s: %w", billable[i].ResourceID, err)
		}
		publishedIDs = append(publishedIDs, billable[i].ResourceID)
	}

	if err := g.store.UpdateLastHeartbeat(ctx, publishedIDs, now); err != nil {
		return fmt.Errorf("updating last heartbeat: %w", err)
	}

	g.logger.Info("heartbeat tick completed", "count", len(publishedIDs))
	return nil
}

func (g *Generator) buildHeartbeatEvent(state *projection.ResourceState, now time.Time) cloudevents.Event {
	ce := cloudevents.NewEvent()
	ce.SetID(uuid.NewString())
	ce.SetSource("osac-metering")
	ce.SetType("osac.resource.heartbeat.v1")
	ce.SetTime(now)

	ce.SetExtension("osacresourceid", state.ResourceID)
	ce.SetExtension("osacresourcetype", state.ResourceType)
	ce.SetExtension("osactenant", state.TenantID)
	if state.ProjectID != "" {
		ce.SetExtension("osacproject", state.ProjectID)
	}

	var durationSeconds float64
	if state.BillableSince != nil {
		durationSeconds = now.Sub(*state.BillableSince).Seconds()
	}

	data := heartbeatData{
		ResourceID:        state.ResourceID,
		ResourceType:      state.ResourceType,
		TenantID:          state.TenantID,
		ProjectID:         events.NilIfEmpty(state.ProjectID),
		CurrentState:      state.CurrentState,
		DurationSeconds:   durationSeconds,
		BillingDimensions: state.BillingDimensions,
		SchemaVersion:     "v1",
	}
	_ = ce.SetData(cloudevents.ApplicationJSON, data)

	return ce
}

func (g *Generator) updateGauges(billable []projection.ResourceState) {
	projectionResources.Reset()
	heartbeatLag.Reset()

	counts := map[string]int{}
	lags := map[string]float64{}
	now := time.Now()
	for i := range billable {
		rt := billable[i].ResourceType
		counts[rt]++
		if billable[i].LastHeartbeatAt != nil {
			lag := now.Sub(*billable[i].LastHeartbeatAt).Seconds()
			if lag > lags[rt] {
				lags[rt] = lag
			}
		}
	}
	for rt, count := range counts {
		projectionResources.WithLabelValues(rt, "true").Set(float64(count))
		heartbeatLag.WithLabelValues(rt).Set(lags[rt])
	}
}

type heartbeatData struct {
	ResourceID        string         `json:"resource_id"`
	ResourceType      string         `json:"resource_type"`
	TenantID          string         `json:"tenant_id"`
	ProjectID         *string        `json:"project_id"`
	CurrentState      string         `json:"current_state"`
	DurationSeconds   float64        `json:"duration_seconds"`
	BillingDimensions map[string]any `json:"billing_dimensions"`
	SchemaVersion     string         `json:"schema_version"`
}
