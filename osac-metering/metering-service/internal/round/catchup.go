/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package round

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/IBM/sarama"
	cloudevents "github.com/cloudevents/sdk-go/v2"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/heartbeat"
	"github.com/osac-project/osac-metering/internal/topics"
	"github.com/osac-project/osac-metering/schema"
)

// caughtHeartbeat is one heartbeat read from the heartbeat topic during catch-up.
type caughtHeartbeat struct {
	id            string
	offset        int64
	roundTime     time.Time
	eventOffset   int64
	key           database.LedgerKey
	tenant        string
	start, end    time.Time
	quantityMicro int64
}

// catchUp reads the heartbeat topic from the tenant's stored heartbeat offset and adds each
// heartbeat's quantity to the ledger, so a database restored from a backup re-sends nothing
// and an empty database replays everything from offset 0. It returns false when it hit its
// batch bound, meaning there is more to read and the round must skip its comparison.
func (r *Round) catchUp(
	ctx context.Context, tx database.Querier, tenant string, progress database.Progress,
) (bool, error) {
	start := time.Time{}
	if progress.LastRoundTime != nil {
		start = *progress.LastRoundTime
	}

	caught, finished, err := r.readHeartbeats(ctx, tenant, progress.HeartbeatOffset)
	if err != nil {
		return false, err
	}
	if len(caught) == 0 {
		return finished, nil
	}
	// A heartbeat whose round time is not after the catch-up start was already folded into
	// this database's own rounds; a duplicate ID within the pass is a resend the relay made
	// after a crash. Both are skipped.
	seen := map[string]bool{}
	last := progress.HeartbeatOffset
	var lastRound *caughtHeartbeat
	for index := range caught {
		entry := &caught[index]
		if !entry.roundTime.After(start) || seen[entry.id] {
			continue
		}
		seen[entry.id] = true
		if err := r.applyHeartbeat(ctx, tx, tenant, entry); err != nil {
			return false, err
		}
		last = entry.offset + 1
		lastRound = entry
	}
	if last != progress.HeartbeatOffset {
		if err := database.AdvanceHeartbeatOffset(ctx, tx, tenant, last); err != nil {
			return false, err
		}
	}
	if lastRound != nil {
		if err := database.SetLastRound(
			ctx, tx, tenant, lastRound.roundTime, lastRound.eventOffset,
		); err != nil {
			return false, err
		}
	}
	return finished, nil
}

// readHeartbeats reads a bounded batch of heartbeats from the tenant's heartbeat topic and
// returns only whole round groups: a group split at the batch bound stays unread, so no round
// is half-applied. It returns false as its second value when more records remain.
func (r *Round) readHeartbeats(
	ctx context.Context, tenant string, offset int64,
) ([]caughtHeartbeat, bool, error) {
	topic := topics.Heartbeat(tenant)
	high, err := r.client.GetOffset(topic, 0, sarama.OffsetNewest)
	if err != nil {
		return nil, false, fmt.Errorf("reading the heartbeat topic end: %w", err)
	}
	if offset >= high {
		return nil, true, nil
	}
	partition, err := r.consumer.ConsumePartition(topic, 0, offset)
	if err != nil {
		return nil, false, fmt.Errorf("consuming %s from %d: %w", topic, offset, err)
	}
	defer func() { _ = partition.Close() }()

	// Read one record beyond the batch bound: it witnesses that the batch's last group
	// ended, without counting towards the bound.
	caught := make([]caughtHeartbeat, 0, r.catchUpBatch+1)
	readCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		<-readCtx.Done()
		_ = partition.Close()
	}()
read:
	for len(caught) < r.catchUpBatch+1 {
		select {
		case <-ctx.Done():
			return nil, false, ctx.Err()
		case message, ok := <-partition.Messages():
			if !ok {
				break read
			}
			heartbeat, err := decodeHeartbeat(message.Value)
			if err != nil {
				return nil, false, fmt.Errorf(
					"decoding heartbeat at %s/%d: %w", topic, message.Offset, err,
				)
			}
			heartbeat.offset = message.Offset
			caught = append(caught, heartbeat)
		case <-time.After(500 * time.Millisecond):
			break read
		}
	}

	// Cut back to the end of the last complete group within the batch.
	applicable := caught
	if len(applicable) > r.catchUpBatch {
		applicable = applicable[:r.catchUpBatch]
	}
	cut := len(applicable)
	if cut > 0 {
		last := applicable[cut-1]
		complete := false
		if cut < len(caught) {
			// A record beyond the batch witnesses the group boundary.
			complete = caught[cut].roundTime != last.roundTime
		} else {
			// No further record arrived: the group is complete only at the topic's
			// end, measured against the high watermark fetched before reading.
			complete = last.offset+1 >= high
		}
		if !complete {
			// Leave the trailing partial group unread: it is not applied, and the
			// offsets do not move past the previous complete group.
			cut = groupStart(applicable, cut-1)
		}
	}
	// Finished only when every applicable record was kept and the read position reached
	// the high watermark fetched before reading.
	finished := cut == len(applicable) && cut > 0 && applicable[cut-1].offset+1 >= high
	return caught[:cut], finished, nil
}

// groupStart returns the index of the first record of the group that the given index belongs
// to.
func groupStart(records []caughtHeartbeat, index int) int {
	for index > 0 && records[index-1].roundTime.Equal(records[index].roundTime) {
		index--
	}
	return index
}

// applyHeartbeat adds the heartbeat's quantity over its interval to the ledger.
func (r *Round) applyHeartbeat(
	ctx context.Context, tx database.Querier, tenant string, caught *caughtHeartbeat,
) error {
	existing, err := database.LedgerPiecesWhere(
		ctx, tx, tenant,
		"resource_type = $2 AND resource_id = $3 AND meter = $4 AND component = $5 AND dimensions = $6",
		caught.key.ResourceType, caught.key.ResourceID, caught.key.Meter,
		caught.key.Component, caught.key.Dimensions,
	)
	if err != nil {
		return err
	}
	pieces := append(existing, database.LedgerPiece{
		Key:           caught.key,
		StartTime:     caught.start,
		EndTime:       caught.end,
		QuantityMicro: caught.quantityMicro,
	})
	return database.ReplaceLedger(ctx, tx, tenant, caught.key, pieces)
}

// decodeHeartbeat reads a heartbeat record from the topic.
func decodeHeartbeat(value []byte) (caughtHeartbeat, error) {
	event := cloudevents.Event{}
	if err := event.UnmarshalJSON(value); err != nil {
		return caughtHeartbeat{}, fmt.Errorf("unmarshaling CloudEvent: %w", err)
	}
	data, err := heartbeat.Data(event)
	if err != nil {
		return caughtHeartbeat{}, err
	}
	from, to, err := heartbeat.Interval(data)
	if err != nil {
		return caughtHeartbeat{}, err
	}
	roundTime, err := heartbeat.HeartbeatTime(data)
	if err != nil {
		return caughtHeartbeat{}, err
	}
	quantity, err := heartbeat.ParseMicro(data.Usage.Quantity)
	if err != nil {
		return caughtHeartbeat{}, err
	}
	dimensions, err := json.Marshal(data.BillingDimensions)
	if err != nil {
		return caughtHeartbeat{}, fmt.Errorf("encoding dimensions: %w", err)
	}
	return caughtHeartbeat{
		id:          event.ID(),
		roundTime:   roundTime,
		eventOffset: data.LastAppliedOffset,
		key: database.LedgerKey{
			ResourceType: schema.SchemaResourceTypeToProto(data.ResourceType),
			ResourceID:   data.ResourceID,
			Meter:        data.Meter,
			Component:    data.Component,
			Dimensions:   string(dimensions),
		},
		tenant:        data.TenantID,
		start:         from,
		end:           to,
		quantityMicro: quantity,
	}, nil
}
