/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

// Package round runs the per-tenant heartbeat rounds. A round catches up, computes expected
// usage from history and rules, compares it with the ledger, and writes the difference,
// positive or negative, to the outbox in the same transaction as the ledger update.
package round

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"time"

	"github.com/IBM/sarama"
	"github.com/go-logr/logr"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/heartbeat"
	"github.com/osac-project/osac-metering/internal/topics"
	"github.com/osac-project/osac-metering/internal/usage"
)

// CatchUpBatch bounds how many heartbeats one catch-up pass reads, so a long backlog applies
// in steps.
const CatchUpBatch = 500

// ClockSkewLimit is how far a pod's clock may differ from PostgreSQL's before rounds are
// skipped.
const ClockSkewLimit = 5 * time.Minute

// Round runs a tenant's heartbeat rounds.
type Round struct {
	store    *database.Store
	engine   *usage.Engine
	client   sarama.Client
	consumer sarama.Consumer
	logger   logr.Logger

	// nowOverride pins the round clock in tests. The production round time comes from
	// PostgreSQL's clock.
	nowOverride func() time.Time

	// catchUpBatch bounds one catch-up pass; tests shrink it.
	catchUpBatch int
}

// New creates a round runner. The client reads topic watermarks; the consumer reads the
// heartbeat topic during catch-up.
func New(
	store *database.Store,
	engine *usage.Engine,
	client sarama.Client,
	consumer sarama.Consumer,
	logger logr.Logger,
) (*Round, error) {
	return &Round{
		store: store, engine: engine, client: client, consumer: consumer, logger: logger,
		catchUpBatch: CatchUpBatch,
	}, nil
}

// SetClock pins the round clock and disables the clock-skew check. For tests only.
func (r *Round) SetClock(clock func() time.Time) {
	r.nowOverride = clock
}

// SetCatchUpBatch shrinks the catch-up bound. For tests only.
func (r *Round) SetCatchUpBatch(size int) {
	r.catchUpBatch = size
}

// RunTenant runs the tenant's rounds until the context ends.
func (r *Round) RunTenant(ctx context.Context, tenant string, interval time.Duration) error {
	if err := r.store.EnsureProgress(ctx, tenant); err != nil {
		return err
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			if err := r.Once(ctx, tenant); err != nil && !errors.Is(err, context.Canceled) {
				r.logger.Error(err, "round failed", "tenant", tenant)
			}
		}
	}
}

// Once runs one round. It skips when catch-up is unfinished, when the fold is behind, when
// PostgreSQL's clock has not passed the last round time, or when the pod's clock is too far
// from PostgreSQL's.
func (r *Round) Once(ctx context.Context, tenant string) error {
	tx, err := r.store.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err := database.TakeTenantLock(ctx, tx, database.LockRound, tenant); err != nil {
		return err
	}

	// The round time comes from PostgreSQL's clock, so all pods agree on it.
	var roundTime time.Time
	if r.nowOverride != nil {
		roundTime = r.nowOverride().UTC()
	} else {
		if err := tx.QueryRow(ctx, "SELECT now()").Scan(&roundTime); err != nil {
			return fmt.Errorf("reading the round clock: %w", err)
		}
		roundTime = roundTime.UTC()
		if delta := time.Since(roundTime); delta > ClockSkewLimit || -delta > ClockSkewLimit {
			r.logger.Error(
				nil, "pod clock is too far from the database clock; skipping round",
				"tenant", tenant, "delta", delta.String(),
			)
			return nil
		}
	}

	progress, err := database.GetProgress(ctx, tx, tenant)
	if err != nil {
		return err
	}
	if progress.LastRoundTime != nil && !roundTime.After(*progress.LastRoundTime) {
		// PostgreSQL's clock moved backwards; wait until it passes the last round.
		return nil
	}

	finished, err := r.catchUp(ctx, tx, tenant, progress)
	if err != nil {
		return err
	}
	if !finished {
		// The offsets and last round move with what catch-up read; the comparison waits.
		return tx.Commit(ctx)
	}
	if progress, err = database.GetProgress(ctx, tx, tenant); err != nil {
		return err
	}

	// A round waits until the tenant's events are all folded, so it never computes from
	// half-folded facts.
	folded, err := r.eventsFolded(ctx, tenant, progress.EventOffset)
	if err != nil {
		return err
	}
	if !folded {
		return nil
	}

	if err := r.compare(ctx, tx, tenant, progress, roundTime); err != nil {
		return err
	}
	if err := database.SetLastRound(ctx, tx, tenant, roundTime, progress.EventOffset); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// eventsFolded reports whether the fold has consumed the event topic up to its end.
func (r *Round) eventsFolded(ctx context.Context, tenant string, offset int64) (bool, error) {
	high, err := r.client.GetOffset(topics.Events(tenant), 0, sarama.OffsetNewest)
	if err != nil {
		return false, fmt.Errorf("reading the event topic end: %w", err)
	}
	if high <= 0 {
		return true, nil
	}
	return offset >= high, nil
}

// resource identifies one billable resource.
type resource struct {
	resourceType string
	resourceID   string
}

// compare recomputes the meters that can have changed and writes the differences.
func (r *Round) compare(
	ctx context.Context,
	tx database.Querier,
	tenant string,
	progress database.Progress,
	roundTime time.Time,
) error {
	resources, err := r.scanResources(ctx, tx, tenant, progress, roundTime)
	if err != nil {
		return err
	}
	for _, resource := range resources {
		facts, err := database.ResourceFacts(ctx, tx, tenant, resource.resourceType, resource.resourceID)
		if err != nil {
			return err
		}
		pieces, err := r.engine.Expected(
			tenant, resource.resourceType, resource.resourceID, facts, roundTime,
		)
		if err != nil {
			return fmt.Errorf(
				"computing usage of %s %s: %w", resource.resourceType, resource.resourceID, err,
			)
		}
		ledger, err := database.LedgerPiecesWhere(
			ctx, tx, tenant, "resource_type = $2 AND resource_id = $3",
			resource.resourceType, resource.resourceID,
		)
		if err != nil {
			return err
		}
		if err := r.diffAndCommit(ctx, tx, tenant, progress, roundTime, pieces, ledger); err != nil {
			return err
		}
	}
	return nil
}

// scanResources returns the resources whose meters can have changed since the previous round:
// resources with new facts, resources still billable at the previous round, and resources of
// types a newly effective revision affects. The first round scans everything, so a round's
// work stays proportional to the heartbeats it sends.
func (r *Round) scanResources(
	ctx context.Context, tx database.Querier, tenant string,
	progress database.Progress, roundTime time.Time,
) ([]resource, error) {
	if progress.LastRoundTime == nil {
		return tenantResources(ctx, tx, tenant, nil)
	}

	seen := map[resource]bool{}
	var result []resource
	add := func(entry resource) {
		if !seen[entry] {
			seen[entry] = true
			result = append(result, entry)
		}
	}

	// Resources with facts the previous round has not used: written after its event
	// offset, or dated after its time, because a writer's clock can run ahead.
	newFacts, err := database.ResourcesWithNewFacts(
		ctx, tx, tenant, progress.LastRoundEventOffset, *progress.LastRoundTime,
	)
	if err != nil {
		return nil, err
	}
	for _, key := range newFacts {
		add(resource{key.ResourceType, key.ResourceID})
	}

	// Meters billable at the previous round: their ledger rows end at its time.
	open, err := database.LedgerPiecesWhere(
		ctx, tx, tenant, "end_time = $2", *progress.LastRoundTime,
	)
	if err != nil {
		return nil, err
	}
	for _, piece := range open {
		add(resource{piece.Key.ResourceType, piece.Key.ResourceID})
	}

	// Meters of resource types a new revision affects, in the round where the revision
	// takes effect.
	affected := map[string]bool{}
	for _, revision := range r.engine.Revisions() {
		if revision.EffectiveAt.After(*progress.LastRoundTime) &&
			!revision.EffectiveAt.After(roundTime) {
			for _, meter := range revision.Meters {
				affected[meter.Resource] = true
			}
		}
	}
	if len(affected) > 0 {
		all, err := tenantResources(ctx, tx, tenant, affected)
		if err != nil {
			return nil, err
		}
		for _, entry := range all {
			add(entry)
		}
	}
	return result, nil
}

func tenantResources(
	ctx context.Context, tx database.Querier, tenant string, types map[string]bool,
) ([]resource, error) {
	keys, err := database.TenantResources(ctx, tx, tenant)
	if err != nil {
		return nil, err
	}
	var result []resource
	for _, key := range keys {
		if types != nil && !types[key.ResourceType] {
			continue
		}
		result = append(result, resource{key.ResourceType, key.ResourceID})
	}
	return result, nil
}

// diffAndCommit compares one resource's expected pieces with its ledger, writes the
// differences to the outbox, and replaces the ledger, all in the round's transaction.
func (r *Round) diffAndCommit(
	ctx context.Context,
	tx database.Querier,
	tenant string,
	progress database.Progress,
	roundTime time.Time,
	expected []usage.Piece,
	ledger []database.LedgerPiece,
) error {
	keys := map[database.LedgerKey]bool{}
	for _, piece := range expected {
		keys[piece.Key] = true
	}
	for _, piece := range ledger {
		keys[piece.Key] = true
	}
	// Sorted, so the heartbeats of one round reach the outbox in a deterministic order.
	ordered := make([]database.LedgerKey, 0, len(keys))
	for key := range keys {
		ordered = append(ordered, key)
	}
	sort.Slice(ordered, func(i, j int) bool {
		left, right := ordered[i], ordered[j]
		if left.Meter != right.Meter {
			return left.Meter < right.Meter
		}
		if left.Component != right.Component {
			return left.Component < right.Component
		}
		return left.Dimensions < right.Dimensions
	})

	for _, key := range ordered {
		differences := diff(key, expectedPiecesOf(expected, key), ledgerPiecesOf(ledger, key))
		rows := make([]database.OutboxRow, 0, len(differences))
		for _, difference := range differences {
			event, err := heartbeat.New(
				tenant, key, difference.start, difference.end, difference.quantityMicro,
				unitOf(expected, key), roundTime, progress.EventOffset,
				revisionOf(expected, key),
			)
			if err != nil {
				return err
			}
			payload, err := event.MarshalJSON()
			if err != nil {
				return fmt.Errorf("marshaling heartbeat: %w", err)
			}
			rows = append(rows, database.OutboxRow{
				ID: event.ID(), Payload: payload, RoundTime: roundTime,
			})
		}
		if err := database.EnqueueHeartbeats(ctx, tx, tenant, rows); err != nil {
			return err
		}
		if err := database.ReplaceLedger(ctx, tx, tenant, key, expectedRows(expected, key)); err != nil {
			return err
		}
	}
	return nil
}

func expectedPiecesOf(pieces []usage.Piece, key database.LedgerKey) []usage.Piece {
	var result []usage.Piece
	for _, piece := range pieces {
		if piece.Key == key {
			result = append(result, piece)
		}
	}
	return result
}

func ledgerPiecesOf(pieces []database.LedgerPiece, key database.LedgerKey) []database.LedgerPiece {
	var result []database.LedgerPiece
	for _, piece := range pieces {
		if piece.Key == key {
			result = append(result, piece)
		}
	}
	return result
}

func unitOf(pieces []usage.Piece, key database.LedgerKey) string {
	for _, piece := range pieces {
		if piece.Key == key {
			return piece.Unit
		}
	}
	return ""
}

func revisionOf(pieces []usage.Piece, key database.LedgerKey) string {
	for _, piece := range pieces {
		if piece.Key == key {
			return piece.Revision
		}
	}
	return ""
}

func expectedRows(pieces []usage.Piece, key database.LedgerKey) []database.LedgerPiece {
	var rows []database.LedgerPiece
	for _, piece := range pieces {
		if piece.Key != key {
			continue
		}
		rows = append(rows, database.LedgerPiece{
			Key:           piece.Key,
			StartTime:     piece.StartTime,
			EndTime:       piece.EndTime,
			QuantityMicro: piece.QuantityMicro(),
		})
	}
	return rows
}

// difference is one heartbeat to send: a quantity over an interval.
type difference struct {
	key           database.LedgerKey
	start         time.Time
	end           time.Time
	quantityMicro int64
	deltaRate     *big.Rat
}

// diff computes the difference between the expected pieces and the ledger rows of one meter.
// Both sides are rate functions; a heartbeat is emitted wherever the rates differ.
func diff(
	key database.LedgerKey, expected []usage.Piece, actual []database.LedgerPiece,
) []difference {
	points := map[time.Time]bool{}
	for _, piece := range expected {
		points[piece.StartTime] = true
		points[piece.EndTime] = true
	}
	for _, piece := range actual {
		points[piece.StartTime] = true
		points[piece.EndTime] = true
	}
	sorted := make([]time.Time, 0, len(points))
	for point := range points {
		sorted = append(sorted, point)
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Before(sorted[j]) })

	var differences []difference
	for i := 1; i < len(sorted); i++ {
		start, end := sorted[i-1], sorted[i]
		if !start.Before(end) {
			continue
		}
		// Rates are micro units per microsecond; the difference over an interval is a
		// quantity in micro units.
		delta := new(big.Rat).Sub(rateAt(expected, start), actualRateAt(actual, start))
		if delta.Sign() == 0 {
			continue
		}
		quantity := new(big.Rat).Mul(delta, big.NewRat(end.Sub(start).Microseconds(), 1))
		micro := usage.RationalToMicro(quantity)
		if micro == 0 {
			continue
		}
		// Adjacent intervals with the same difference are one piece of usage.
		if last := len(differences) - 1; last >= 0 &&
			differences[last].end.Equal(start) && differences[last].rate().Cmp(delta) == 0 {
			differences[last].end = end
			differences[last].quantityMicro += micro
			continue
		}
		differences = append(differences, difference{
			key: key, start: start, end: end, quantityMicro: micro, deltaRate: delta,
		})
	}
	return differences
}

// rate returns the difference rate the piece was built from.
func (d difference) rate() *big.Rat { return d.deltaRate }

// rateAt returns the expected rate at a moment, in micro units per microsecond. A pending
// piece commits nothing.
func rateAt(pieces []usage.Piece, moment time.Time) *big.Rat {
	for _, piece := range pieces {
		if !moment.Before(piece.StartTime) && moment.Before(piece.EndTime) {
			if piece.Pending {
				return new(big.Rat)
			}
			return big.NewRat(piece.RateMicro, 1_000_000)
		}
	}
	return new(big.Rat)
}

// actualRateAt returns the ledger's committed rate at a moment, in micro units per
// microsecond.
func actualRateAt(pieces []database.LedgerPiece, moment time.Time) *big.Rat {
	for _, piece := range pieces {
		if !moment.Before(piece.StartTime) && moment.Before(piece.EndTime) {
			duration := piece.EndTime.Sub(piece.StartTime).Microseconds()
			if duration <= 0 {
				continue
			}
			return new(big.Rat).SetFrac(big.NewInt(piece.QuantityMicro), big.NewInt(duration))
		}
	}
	return new(big.Rat)
}
