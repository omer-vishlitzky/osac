/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package round_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/IBM/sarama"
	cloudevents "github.com/cloudevents/sdk-go/v2"
	"github.com/go-logr/logr"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/fold"
	"github.com/osac-project/osac-metering/internal/heartbeat"
	"github.com/osac-project/osac-metering/internal/relay"
	"github.com/osac-project/osac-metering/internal/round"
	"github.com/osac-project/osac-metering/internal/rules"
	"github.com/osac-project/osac-metering/internal/testenv"
	"github.com/osac-project/osac-metering/internal/topics"
	"github.com/osac-project/osac-metering/internal/usage"
	"github.com/osac-project/osac-metering/schema"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
)

// sentBeat is a heartbeat read from a topic, with its ID.
type sentBeat struct {
	id   string
	data schema.HeartbeatData
}

func TestMain(m *testing.M) {
	code := m.Run()
	testenv.StopContainers()
	os.Exit(code)
}

var base = time.Date(2026, 10, 5, 10, 0, 0, 0, time.UTC)

// pipeline bundles a fold, a round, and a relay against one database, with a clock the test
// controls.
type pipeline struct {
	tenant string
	store  *database.Store
	fold   *fold.Fold
	round  *round.Round
	relay  *relay.Relay
}

func newPipeline(t *testing.T, tenant string) *pipeline {
	t.Helper()
	return newPipelineOnStore(t, tenant, testenv.Database(t))
}

func newPipelineOnStore(t *testing.T, tenant string, store *database.Store) *pipeline {
	t.Helper()
	revision, err := rules.DefaultRevision()
	if err != nil {
		t.Fatalf("parsing default revision: %v", err)
	}
	engine, err := usage.NewEngine([]rules.Revision{revision}, "us-east")
	if err != nil {
		t.Fatalf("creating engine: %v", err)
	}
	foldLoop, err := fold.New(store, testenv.NewConsumer(t), logr.Discard())
	if err != nil {
		t.Fatalf("creating fold: %v", err)
	}
	roundLoop, err := round.New(
		store, engine, testenv.NewClient(t), testenv.NewConsumer(t), logr.Discard(),
	)
	if err != nil {
		t.Fatalf("creating round: %v", err)
	}
	relayLoop, err := relay.New(store, testenv.NewProducer(t), logr.Discard())
	if err != nil {
		t.Fatalf("creating relay: %v", err)
	}
	return &pipeline{tenant: tenant, store: store, fold: foldLoop, round: roundLoop, relay: relayLoop}
}

// startRelay runs the relay in the background for the rest of the test.
func (p *pipeline) startRelay(t *testing.T) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = p.relay.RunTenant(ctx, p.tenant) }()
}

// foldAll folds until the tenant's stored offset reaches total.
func (p *pipeline) foldAll(t *testing.T, total int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	go func() { _ = p.fold.RunTenant(ctx, p.tenant) }()
	deadline := time.Now().Add(30 * time.Second)
	for {
		progress, err := p.store.GetTenantProgress(context.Background(), p.tenant)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			t.Fatalf("reading progress: %v", err)
		}
		if progress.EventOffset >= int64(total) {
			cancel()
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("fold did not reach offset %d, stopped at %d", total, progress.EventOffset)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// roundAt runs one round at a fixed time.
func (p *pipeline) roundAt(t *testing.T, moment time.Time) {
	t.Helper()
	p.round.SetClock(func() time.Time { return moment })
	if err := p.round.Once(context.Background(), p.tenant); err != nil {
		t.Fatalf("round at %s: %v", moment, err)
	}
}

// waitOutboxEmpty waits until the relay has sent every heartbeat the rounds wrote.
func (p *pipeline) waitOutboxEmpty(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		rows, err := database.NextOutbox(context.Background(), p.store.Pool(), p.tenant, 1)
		if err != nil {
			t.Fatalf("reading outbox: %v", err)
		}
		if len(rows) == 0 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("relay did not drain the outbox")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// sentHeartbeats reads every heartbeat the relay put on the tenant's heartbeat topic.
func sentHeartbeats(t *testing.T, tenant string) []sentBeat {
	t.Helper()
	var result []sentBeat
	for _, value := range testenv.ConsumeAll(t, topics.Heartbeat(tenant)) {
		event := cloudevents.Event{}
		if err := event.UnmarshalJSON(value); err != nil {
			t.Fatalf("unmarshaling heartbeat: %v", err)
		}
		data, err := heartbeat.Data(event)
		if err != nil {
			t.Fatalf("decoding heartbeat: %v", err)
		}
		result = append(result, sentBeat{id: event.ID(), data: data})
	}
	return result
}

// quantities renders the heartbeats as comparable text.
func quantities(beats []sentBeat) []string {
	var lines []string
	for _, beat := range beats {
		lines = append(lines, fmt.Sprintf(
			"%s %s..%s %s", beat.data.Meter, beat.data.Usage.From, beat.data.Usage.To,
			beat.data.Usage.Quantity,
		))
	}
	return lines
}

// TestLateEventSendsTheCorrection replays the design's example: a VM that stops, but the
// update reaches fulfillment late.
func TestLateEventSendsTheCorrection(t *testing.T) {
	p := newPipeline(t, "late")
	p.startRelay(t)

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "STARTING", base, "small", base),
		testenv.VMEvent("e2", privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED,
			base.Add(40*time.Second), base, "web", "RUNNING",
			base.Add(40*time.Second), "small", base),
	})
	p.foldAll(t, total)

	p.roundAt(t, base.Add(time.Minute))
	p.roundAt(t, base.Add(2*time.Minute))
	p.roundAt(t, base.Add(3*time.Minute))
	p.waitOutboxEmpty(t)

	// The stop reaches fulfillment late: it happened at 10:02:30 but arrives after the
	// 10:03 round.
	total += testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e4", privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED,
			base.Add(3*time.Minute+10*time.Second), base, "web", "STOPPED",
			base.Add(2*time.Minute+30*time.Second), "small", base),
	})
	p.foldAll(t, total)
	p.roundAt(t, base.Add(4*time.Minute))
	p.waitOutboxEmpty(t)

	got := quantities(sentHeartbeats(t, p.tenant))
	want := []string{
		"compute 2026-10-05T10:00:40Z..2026-10-05T10:01:00Z 20.000000",
		"compute 2026-10-05T10:01:00Z..2026-10-05T10:02:00Z 60.000000",
		"compute 2026-10-05T10:02:00Z..2026-10-05T10:03:00Z 60.000000",
		"compute 2026-10-05T10:02:30Z..2026-10-05T10:03:00Z -30.000000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// TestRoundLevelSubSecondRun checks the round-level behavior: a run still under one second
// at the first round sends nothing, and the next round bills the whole run from its start,
// including the part the first round watched as pending.
func TestRoundLevelSubSecondRun(t *testing.T) {
	p := newPipeline(t, "subsecond")
	p.startRelay(t)

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "RUNNING", base, "small", base),
	})
	p.foldAll(t, total)

	// The run is 0.4 s long at the first round: nothing is billable yet.
	p.roundAt(t, base.Add(400*time.Millisecond))
	if outboxCount(t, p.store, p.tenant) != 0 {
		t.Fatal("a run under one second must not send a heartbeat")
	}

	// 1.4 s later the run is 1.8 s long: the round bills 1.8 s from the run's start.
	p.roundAt(t, base.Add(1800*time.Millisecond))
	p.waitOutboxEmpty(t)

	got := quantities(sentHeartbeats(t, p.tenant))
	want := []string{
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:00:01.8Z 1.800000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// TestRestoreSendsNothingTwice simulates a database restored from a backup: the restored
// ledger is behind the heartbeat topic, and catch-up brings it up to date before the round
// compares.
func TestRestoreSendsNothingTwice(t *testing.T) {
	const tenant = "restore"
	p := newPipeline(t, tenant)
	p.startRelay(t)

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "RUNNING", base, "small", base),
	})
	p.foldAll(t, total)

	// Backup point: after the 10:01 round and its relay.
	p.roundAt(t, base.Add(time.Minute))
	p.waitOutboxEmpty(t)
	backup := snapshot(t, p.store, p.tenant)

	// The live system continues without the backup.
	p.roundAt(t, base.Add(2*time.Minute))
	p.roundAt(t, base.Add(3*time.Minute))
	p.waitOutboxEmpty(t)

	// The restored database knows only the backup. Catch-up reads the heartbeats sent
	// after the backup, and the round sends only the difference.
	restored := newPipelineOnStore(t, p.tenant, restore(t, backup, p.tenant))
	restored.startRelay(t)
	restored.roundAt(t, base.Add(4*time.Minute))
	restored.waitOutboxEmpty(t)

	got := quantities(sentHeartbeats(t, p.tenant))
	want := []string{
		// Before the restore.
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:01:00Z 60.000000",
		"compute 2026-10-05T10:01:00Z..2026-10-05T10:02:00Z 60.000000",
		"compute 2026-10-05T10:02:00Z..2026-10-05T10:03:00Z 60.000000",
		// After the restore: only 10:03 to 10:04.
		"compute 2026-10-05T10:03:00Z..2026-10-05T10:04:00Z 60.000000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// TestTwoPodsCatchUpApplyEachHeartbeatOnce runs two rounds concurrently on a restored
// database; the round lock and the ID checks keep each heartbeat applied once.
func TestTwoPodsCatchUpApplyEachHeartbeatOnce(t *testing.T) {
	const tenant = "twopods"
	p := newPipeline(t, tenant)
	p.startRelay(t)

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "RUNNING", base, "small", base),
	})
	p.foldAll(t, total)

	p.roundAt(t, base.Add(time.Minute))
	p.waitOutboxEmpty(t)
	backup := snapshot(t, p.store, p.tenant)
	p.roundAt(t, base.Add(2*time.Minute))
	p.roundAt(t, base.Add(3*time.Minute))
	p.waitOutboxEmpty(t)

	restoredStore := restore(t, backup, p.tenant)
	revision, err := rules.DefaultRevision()
	if err != nil {
		t.Fatalf("parsing default revision: %v", err)
	}
	engine, err := usage.NewEngine([]rules.Revision{revision}, "us-east")
	if err != nil {
		t.Fatalf("creating engine: %v", err)
	}

	// Two pods run a round at the same time on the restored database.
	var failures []error
	done := make(chan struct{})
	for i := range 2 {
		pod, err := round.New(
			restoredStore, engine, testenv.NewClient(t), testenv.NewConsumer(t), logr.Discard(),
		)
		if err != nil {
			t.Fatalf("creating round: %v", err)
		}
		pod.SetClock(func() time.Time { return base.Add(4 * time.Minute) })
		go func() {
			if err := pod.Once(context.Background(), tenant); err != nil {
				failures = append(failures, err)
			}
			done <- struct{}{}
		}()
		_ = i
	}
	for range 2 {
		<-done
	}
	if len(failures) > 0 {
		t.Fatalf("rounds failed: %v", failures)
	}

	// Relay the restored database's outbox with a second relay loop, so both pods' work
	// reaches the topic.
	restoredRelay, err := relay.New(restoredStore, testenv.NewProducer(t), logr.Discard())
	if err != nil {
		t.Fatalf("creating relay: %v", err)
	}
	relayCtx, relayCancel := context.WithCancel(context.Background())
	go func() { _ = restoredRelay.RunTenant(relayCtx, tenant) }()
	deadline := time.Now().Add(30 * time.Second)
	for {
		rows, err := database.NextOutbox(context.Background(), restoredStore.Pool(), tenant, 1)
		if err != nil {
			t.Fatalf("reading outbox: %v", err)
		}
		if len(rows) == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("relay did not drain the restored outbox")
		}
		time.Sleep(20 * time.Millisecond)
	}
	relayCancel()

	got := quantities(sentHeartbeats(t, p.tenant))
	want := []string{
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:01:00Z 60.000000",
		"compute 2026-10-05T10:01:00Z..2026-10-05T10:02:00Z 60.000000",
		"compute 2026-10-05T10:02:00Z..2026-10-05T10:03:00Z 60.000000",
		"compute 2026-10-05T10:03:00Z..2026-10-05T10:04:00Z 60.000000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// TestRelayResendKeepsIDs simulates a crash between the Kafka send and the outbox delete: the
// same rows are sent again with the same IDs, and catch-up on a fresh database applies each
// once.
func TestRelayResendKeepsIDs(t *testing.T) {
	p := newPipeline(t, "resend")

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "RUNNING", base, "small", base),
	})
	p.foldAll(t, total)

	// The round writes its heartbeat. The relay is not running yet.
	p.roundAt(t, base.Add(time.Minute))
	rows, err := database.NextOutbox(context.Background(), p.store.Pool(), p.tenant, 10)
	if err != nil {
		t.Fatalf("reading outbox: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected one outbox row, got %d", len(rows))
	}

	// The crash: the rows reach the topic but the delete never commits. The relay then
	// starts, sends them again with the same IDs, and deletes them.
	producer := testenv.NewProducer(t)
	for _, row := range rows {
		message := &sarama.ProducerMessage{
			Topic: topics.Heartbeat(p.tenant),
			Key:   sarama.StringEncoder(row.ID),
			Value: sarama.ByteEncoder(row.Payload),
		}
		if _, _, err := producer.SendMessage(message); err != nil {
			t.Fatalf("resending: %v", err)
		}
	}
	p.startRelay(t)
	p.waitOutboxEmpty(t)

	// A fresh database replays everything from offset 0. The duplicate on the topic
	// carries the same ID, so catch-up counts it once.
	fresh := newPipelineOnStore(t, p.tenant, testenv.Database(t))
	fresh.startRelay(t)
	fresh.foldAll(t, total)
	fresh.roundAt(t, base.Add(2*time.Minute))
	fresh.waitOutboxEmpty(t)

	got := quantities(sentHeartbeats(t, p.tenant))
	want := []string{
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:01:00Z 60.000000",
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:01:00Z 60.000000",
		"compute 2026-10-05T10:01:00Z..2026-10-05T10:02:00Z 60.000000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}

	// The duplicate on the topic carries the same ID.
	beats := sentHeartbeats(t, p.tenant)
	if beats[0].id != beats[1].id {
		t.Fatalf("resent heartbeats must keep their IDs: %s vs %s", beats[0].id, beats[1].id)
	}
}

// TestBoundedCatchUp checks that a long backlog applies in bounded steps, that the round
// skips its comparison until catch-up finishes, and that a round group split across the batch
// boundary is applied whole: no heartbeat twice, none skipped.
func TestBoundedCatchUp(t *testing.T) {
	p := newPipeline(t, "bounded")
	p.startRelay(t)

	total := testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e1", privatev1.EventType_EVENT_TYPE_OBJECT_CREATED,
			base, base, "web", "RUNNING", base, "small", base),
	})
	p.foldAll(t, total)

	// The backup knows the first round. After it, the VM is resized mid-minute, so the
	// 10:03 round sends a group of two heartbeats: small until the resize, large after.
	p.roundAt(t, base.Add(time.Minute))
	p.waitOutboxEmpty(t)
	backup := snapshot(t, p.store, p.tenant)
	total += testenv.ProduceEvents(t, p.tenant, []*privatev1.Event{
		testenv.VMEvent("e2", privatev1.EventType_EVENT_TYPE_OBJECT_UPDATED,
			base.Add(3*time.Minute), base, "web", "RUNNING", base, "large",
			base.Add(2*time.Minute+30*time.Second)),
	})
	p.foldAll(t, total)
	p.roundAt(t, base.Add(2*time.Minute))
	p.roundAt(t, base.Add(3*time.Minute))
	p.waitOutboxEmpty(t)

	restored := newPipelineOnStore(t, p.tenant, restore(t, backup, p.tenant))
	restored.foldAll(t, total)
	restored.round.SetCatchUpBatch(2)

	// The first restored round reads the 10:02 heartbeat and the first half of the
	// 10:03 group. The group is split at the batch bound, so only the complete 10:02
	// group applies and nothing is half-applied.
	restored.roundAt(t, base.Add(4*time.Minute))
	if outboxCount(t, restored.store, p.tenant) != 0 {
		t.Fatal("a round with unfinished catch-up must not write heartbeats")
	}
	small, err := database.LedgerPiecesWhere(
		context.Background(), restored.store.Pool(), p.tenant, "meter = $2", "compute",
	)
	if err != nil {
		t.Fatalf("reading restored ledger: %v", err)
	}
	for _, piece := range small {
		if piece.EndTime.After(base.Add(2 * time.Minute)) {
			t.Fatalf(
				"a group split at the batch bound must stay unread, got a piece ending at %s",
				piece.EndTime,
			)
		}
	}

	// The second pass reads the whole 10:03 group, applies it, and the round sends only
	// the new interval.
	restored.roundAt(t, base.Add(5*time.Minute))
	if outboxCount(t, restored.store, p.tenant) == 0 {
		t.Fatal("a finished catch-up must let the round send its difference")
	}
	restored.startRelay(t)
	restored.waitOutboxEmpty(t)

	// Nothing twice, nothing skipped: the small meter sums to 10:00 to 10:02:30 and the
	// large meter to 10:02:30 to 10:05.
	got := quantities(sentHeartbeats(t, p.tenant))
	// Within the 10:03 group the meters arrive in the round's deterministic key order,
	// which sorts the large dimension set before the small one.
	want := []string{
		"compute 2026-10-05T10:00:00Z..2026-10-05T10:01:00Z 60.000000",
		"compute 2026-10-05T10:01:00Z..2026-10-05T10:02:00Z 60.000000",
		"compute 2026-10-05T10:02:30Z..2026-10-05T10:03:00Z 30.000000",
		"compute 2026-10-05T10:02:00Z..2026-10-05T10:02:30Z 30.000000",
		"compute 2026-10-05T10:03:00Z..2026-10-05T10:05:00Z 120.000000",
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("heartbeats mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// snapshot copies a tenant's rows, as a backup would.
type tenantSnapshot struct {
	history  []database.Fact
	ledger   []database.LedgerPiece
	progress database.Progress
}

func snapshot(t *testing.T, store *database.Store, tenant string) tenantSnapshot {
	t.Helper()
	ctx := context.Background()
	progress, err := store.GetTenantProgress(ctx, tenant)
	if err != nil {
		t.Fatalf("reading progress: %v", err)
	}
	resources, err := database.TenantResources(ctx, store.Pool(), tenant)
	if err != nil {
		t.Fatalf("listing resources: %v", err)
	}
	var snap tenantSnapshot
	snap.progress = progress
	for _, resource := range resources {
		facts, err := database.ResourceFacts(
			ctx, store.Pool(), tenant, resource.ResourceType, resource.ResourceID,
		)
		if err != nil {
			t.Fatalf("reading facts: %v", err)
		}
		snap.history = append(snap.history, facts...)
	}
	ledger, err := store.LedgerPieces(ctx, tenant)
	if err != nil {
		t.Fatalf("reading ledger: %v", err)
	}
	snap.ledger = ledger
	return snap
}

// restore writes a snapshot into a fresh database, as a restore would.
func restore(t *testing.T, snap tenantSnapshot, tenant string) *database.Store {
	t.Helper()
	store := testenv.Database(t)
	ctx := context.Background()
	if err := store.EnsureProgress(ctx, tenant); err != nil {
		t.Fatalf("ensuring progress: %v", err)
	}
	tx, err := store.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := database.InsertFacts(ctx, tx, tenant, snap.history, snap.progress.EventOffset); err != nil {
		t.Fatalf("restoring history: %v", err)
	}
	for _, piece := range snap.ledger {
		if err := database.ReplaceLedger(
			ctx, tx, tenant, piece.Key, []database.LedgerPiece{piece},
		); err != nil {
			t.Fatalf("restoring ledger: %v", err)
		}
	}
	if _, err := tx.Exec(
		ctx,
		`UPDATE progress SET heartbeat_offset = $2, last_round_time = $3,
		 last_round_event_offset = $4 WHERE tenant = $1`,
		tenant, snap.progress.HeartbeatOffset, snap.progress.LastRoundTime,
		snap.progress.LastRoundEventOffset,
	); err != nil {
		t.Fatalf("restoring progress: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}
	return store
}

func outboxCount(t *testing.T, store *database.Store, tenant string) int {
	t.Helper()
	rows, err := database.NextOutbox(context.Background(), store.Pool(), tenant, 1000)
	if err != nil {
		t.Fatalf("reading outbox: %v", err)
	}
	return len(rows)
}

func join(lines []string) string {
	out := ""
	for _, line := range lines {
		out += line + "\n"
	}
	return out
}
