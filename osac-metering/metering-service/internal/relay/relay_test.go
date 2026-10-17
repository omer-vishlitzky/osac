/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package relay

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/IBM/sarama"
	"github.com/go-logr/logr"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/heartbeat"
	"github.com/osac-project/osac-metering/internal/testenv"
	"github.com/osac-project/osac-metering/internal/topics"
)

func TestMain(m *testing.M) {
	code := m.Run()
	testenv.StopContainers()
	os.Exit(code)
}

var base = time.Date(2026, 10, 5, 10, 0, 0, 0, time.UTC)

func key(meter string) database.LedgerKey {
	return database.LedgerKey{
		ResourceType: "ComputeInstance",
		ResourceID:   "vm-7",
		Meter:        meter,
		Component:    meter,
		Dimensions:   `{"tenant":"acme"}`,
	}
}

// TestDrainOnceStoresTheSentOffset sends a multi-heartbeat batch and checks the stored
// heartbeat offset: it must move past every record the relay sent, not past the partition
// number of the first send.
func TestDrainOnceStoresTheSentOffset(t *testing.T) {
	tenant := "relay-offset"
	store := testenv.Database(t)
	if err := store.EnsureProgress(context.Background(), tenant); err != nil {
		t.Fatalf("ensuring progress: %v", err)
	}
	if err := testenv.Kafka(t).CreateTopic(topics.Heartbeat(tenant)); err != nil {
		t.Fatalf("creating topic: %v", err)
	}

	// Five heartbeats, one per minute, all from one round.
	roundTime := base.Add(time.Minute)
	rows := make([]database.OutboxRow, 0, 5)
	for i := range 5 {
		event, err := heartbeat.New(
			tenant, key("compute"), base.Add(time.Duration(i)*time.Minute),
			base.Add(time.Duration(i+1)*time.Minute), 60_000_000, "second",
			roundTime, int64(i), "0001",
		)
		if err != nil {
			t.Fatalf("building heartbeat: %v", err)
		}
		payload, err := event.MarshalJSON()
		if err != nil {
			t.Fatalf("marshaling heartbeat: %v", err)
		}
		rows = append(rows, database.OutboxRow{
			ID: event.ID(), Payload: payload, RoundTime: roundTime,
		})
	}
	if err := database.EnqueueHeartbeats(context.Background(), store.Pool(), tenant, rows); err != nil {
		t.Fatalf("enqueueing: %v", err)
	}

	relay, err := New(store, testenv.NewProducer(t), logr.Discard())
	if err != nil {
		t.Fatalf("creating relay: %v", err)
	}
	sent, err := relay.drainOnce(context.Background(), tenant, topics.Heartbeat(tenant))
	if err != nil {
		t.Fatalf("draining: %v", err)
	}
	if sent != len(rows) {
		t.Fatalf("expected %d sent heartbeats, got %d", len(rows), sent)
	}

	progress, err := store.GetTenantProgress(context.Background(), tenant)
	if err != nil {
		t.Fatalf("reading progress: %v", err)
	}
	// The topic held no records before this batch, so the five sends take offsets 0 to 4
	// and the stored next offset is 5.
	if progress.HeartbeatOffset != 5 {
		t.Fatalf("stored heartbeat offset is %d, want 5", progress.HeartbeatOffset)
	}

	remaining, err := database.NextOutbox(context.Background(), store.Pool(), tenant, 10)
	if err != nil {
		t.Fatalf("reading outbox: %v", err)
	}
	if len(remaining) != 0 {
		t.Fatalf("outbox still holds %d rows after the drain", len(remaining))
	}
}

// TestRunTenantSurvivesSendErrors checks that a transient send error does not stop the relay:
// the loop logs it and drains again once the broker is back.
func TestRunTenantSurvivesSendErrors(t *testing.T) {
	tenant := "relay-resilient"
	store := testenv.Database(t)
	if err := store.EnsureProgress(context.Background(), tenant); err != nil {
		t.Fatalf("ensuring progress: %v", err)
	}
	if err := testenv.Kafka(t).CreateTopic(topics.Heartbeat(tenant)); err != nil {
		t.Fatalf("creating topic: %v", err)
	}

	roundTime := base.Add(time.Minute)
	event, err := heartbeat.New(
		tenant, key("compute"), base, base.Add(time.Minute), 60_000_000, "second",
		roundTime, 0, "0001",
	)
	if err != nil {
		t.Fatalf("building heartbeat: %v", err)
	}
	payload, err := event.MarshalJSON()
	if err != nil {
		t.Fatalf("marshaling heartbeat: %v", err)
	}
	if err := database.EnqueueHeartbeats(context.Background(), store.Pool(), tenant,
		[]database.OutboxRow{{ID: event.ID(), Payload: payload, RoundTime: roundTime}},
	); err != nil {
		t.Fatalf("enqueueing: %v", err)
	}

	// A producer whose broker went away fails every send; the relay must keep looping
	// rather than exit. The heartbeat is still in the outbox afterwards.
	dead, err := New(store, failingProducer{}, logr.Discard())
	if err != nil {
		t.Fatalf("creating relay: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = dead.RunTenant(ctx, tenant) }()
	time.Sleep(300 * time.Millisecond)
	cancel()
	remaining, err := database.NextOutbox(context.Background(), store.Pool(), tenant, 10)
	if err != nil {
		t.Fatalf("reading outbox: %v", err)
	}
	if len(remaining) != 1 {
		t.Fatalf("expected the outbox to keep the heartbeat, got %d rows", len(remaining))
	}

	// The real relay then drains the same outbox.
	relay, err := New(store, testenv.NewProducer(t), logr.Discard())
	if err != nil {
		t.Fatalf("creating relay: %v", err)
	}
	sent, err := relay.drainOnce(context.Background(), tenant, topics.Heartbeat(tenant))
	if err != nil {
		t.Fatalf("draining after failures: %v", err)
	}
	if sent != 1 {
		t.Fatalf("expected the relay to send the queued heartbeat after failures, got %d", sent)
	}
}

// failingProducer fails every send, like a producer whose broker went away. The transaction
// methods exist only to satisfy the sarama.SyncProducer interface.
type failingProducer struct{}

func (failingProducer) SendMessage(*sarama.ProducerMessage) (int32, int64, error) {
	return 0, 0, errors.New("broker unreachable")
}

func (failingProducer) SendMessages([]*sarama.ProducerMessage) error {
	return errors.New("broker unreachable")
}

func (failingProducer) Close() error                            { return nil }
func (failingProducer) TxnStatus() sarama.ProducerTxnStatusFlag { return 0 }
func (failingProducer) BeginTxn() error                         { return nil }
func (failingProducer) CommitTxn() error                        { return nil }
func (failingProducer) AbortTxn() error                         { return nil }
func (failingProducer) AddOffsetsToTxn(map[string][]*sarama.PartitionOffsetMetadata, string) error {
	return nil
}

func (failingProducer) AddMessageToTxn(*sarama.ConsumerMessage, string, *string) error {
	return nil
}

func (failingProducer) IsTransactional() bool { return false }

func (failingProducer) AddOffsetsToTxnWithGroupMetadata(
	map[string][]*sarama.PartitionOffsetMetadata, *sarama.ConsumerGroupMetadata,
) error {
	return nil
}

func (failingProducer) AddMessageToTxnWithGroupMetadata(
	*sarama.ConsumerMessage, *sarama.ConsumerGroupMetadata, *string,
) error {
	return nil
}
