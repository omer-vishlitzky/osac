/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package usage_test

import (
	"fmt"
	"math/big"
	"testing"
	"time"

	"github.com/osac-project/osac-metering/internal/database"
	"github.com/osac-project/osac-metering/internal/rules"
	"github.com/osac-project/osac-metering/internal/usage"
)

var base = time.Date(2026, 10, 5, 10, 0, 0, 0, time.UTC)

func defaultEngine(t *testing.T) *usage.Engine {
	t.Helper()
	revision, err := rules.DefaultRevision()
	if err != nil {
		t.Fatalf("parsing default revision: %v", err)
	}
	engine, err := usage.NewEngine([]rules.Revision{revision}, "us-east")
	if err != nil {
		t.Fatalf("creating engine: %v", err)
	}
	return engine
}

func fact(resource, id, field string, since time.Time, value string) database.Fact {
	return database.Fact{
		ResourceType: resource, ResourceID: id, Field: field, Since: since, Value: value,
	}
}

func created(resource, id string, at time.Time) database.Fact {
	return fact(resource, id, "created", at, "true")
}

func state(resource, id string, since time.Time, value string) database.Fact {
	return fact(resource, id, "status.state", since, value)
}

// summarize renders pieces as comparable text.
func summarize(pieces []usage.Piece) []string {
	var lines []string
	for _, piece := range pieces {
		lines = append(lines, fmt.Sprintf(
			"%s %s %s..%s rate=%d micro=%d pending=%v dims=%s",
			piece.Key.ResourceType, piece.Key.Meter,
			piece.StartTime.Format("15:04:05.000000"), piece.EndTime.Format("15:04:05.000000"),
			piece.RateMicro, piece.QuantityMicro(), piece.Pending, piece.Key.Dimensions,
		))
	}
	return lines
}

func expectPieces(
	t *testing.T, engine *usage.Engine, facts []database.Fact, upTo time.Time, want []string,
) {
	t.Helper()
	pieces, err := engine.Expected("acme", facts[0].ResourceType, facts[0].ResourceID, facts, upTo)
	if err != nil {
		t.Fatalf("computing usage: %v", err)
	}
	got := summarize(pieces)
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("pieces mismatch:\ngot:\n%s\nwant:\n%s", join(got), join(want))
	}
}

// TestRationalToMicroLeavesItsArgumentAlone checks that negation for rounding works on a
// copy: a caller that reuses the quantity must not see its sign flip.
func TestRationalToMicroLeavesItsArgumentAlone(t *testing.T) {
	quantity := big.NewRat(-3, 2)
	if got := usage.RationalToMicro(quantity); got != -2 {
		t.Errorf("-1.5 micro units must round to -2, got %d", got)
	}
	if quantity.Cmp(big.NewRat(-3, 2)) != 0 {
		t.Errorf("RationalToMicro mutated its argument to %s", quantity.RatString())
	}
}

// TestVolumeCapacityRejectsBadMultipliers checks that a negative or out-of-range multiplier
// fails the evaluation instead of wrapping into a wrong rate.
func TestVolumeCapacityRejectsBadMultipliers(t *testing.T) {
	engine := defaultEngine(t)
	volume := []database.Fact{
		created("Volume", "vol-1", at(0)),
		state("Volume", "vol-1", at(0), "AVAILABLE"),
		fact("Volume", "vol-1", "status.protocol", at(0), "BLOCK"),
		fact("Volume", "vol-1", "status.vendor_volume_id", at(0), "vend-9"),
	}
	for _, value := range []string{"-5", "9223372036856"} {
		facts := append(append([]database.Fact(nil), volume...),
			fact("Volume", "vol-1", "status.provisioned_size_gib", at(0), value),
		)
		if _, err := engine.Expected("acme", "Volume", "vol-1", facts, at(time.Minute)); err == nil {
			t.Errorf("a multiplier of %s must fail the evaluation", value)
		}
	}
}

func join(lines []string) string {
	out := ""
	for _, line := range lines {
		out += line + "\n"
	}
	return out
}

func at(offset time.Duration) time.Time { return base.Add(offset) }

func TestComputeInstanceBillsOnlyRunning(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(0), "STARTING"),
		state("ComputeInstance", "vm-7", at(10*time.Second), "RUNNING"),
		fact("ComputeInstance", "vm-7", "status.instance_type", at(0), "small"),
		fact("ComputeInstance", "vm-7", "metadata.project", at(0), "web"),
	}
	expectPieces(t, engine, facts, at(70*time.Second), []string{
		"ComputeInstance compute 10:00:10.000000..10:01:10.000000 rate=1000000 micro=60000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"instance_type\":\"small\"," +
			"\"meter_type\":\"second\",\"project\":\"web\",\"tenant\":\"acme\"}",
	})
}

func TestComputeInstanceStopsAtNonRunningStates(t *testing.T) {
	engine := defaultEngine(t)
	for _, terminal := range []string{"STOPPED", "PAUSED", "FAILED", "DELETING"} {
		facts := []database.Fact{
			created("ComputeInstance", "vm-7", at(0)),
			state("ComputeInstance", "vm-7", at(0), "RUNNING"),
			state("ComputeInstance", "vm-7", at(30*time.Second), terminal),
		}
		expectPieces(t, engine, facts, at(time.Minute), []string{
			"ComputeInstance compute 10:00:00.000000..10:00:30.000000 rate=1000000 micro=30000000 " +
				"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
				"\"tenant\":\"acme\"}",
		})
	}
}

func TestRunUnderOneSecondBillsNothing(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(200*time.Millisecond), "RUNNING"),
		state("ComputeInstance", "vm-7", at(600*time.Millisecond), "STOPPED"),
	}
	expectPieces(t, engine, facts, at(time.Minute), nil)
}

func TestOpenRunUnderOneSecondStaysPending(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(200*time.Millisecond), "RUNNING"),
	}
	// The run is open and 400 ms long at the round time: nothing is billable yet, and the
	// piece is pending so the next round keeps watching it.
	expectPieces(t, engine, facts, at(600*time.Millisecond), []string{
		"ComputeInstance compute 10:00:00.200000..10:00:00.600000 rate=1000000 micro=0 " +
			"pending=true dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
	// Once the run crosses one second it bills from its start.
	expectPieces(t, engine, facts, at(2*time.Second), []string{
		"ComputeInstance compute 10:00:00.200000..10:00:02.000000 rate=1000000 micro=1800000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}

func TestDurationIsExactToTheMicrosecond(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(200*time.Millisecond), "RUNNING"),
		state("ComputeInstance", "vm-7", at(2800*time.Millisecond), "STOPPED"),
	}
	expectPieces(t, engine, facts, at(time.Minute), []string{
		"ComputeInstance compute 10:00:00.200000..10:00:02.800000 rate=1000000 micro=2600000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}

func TestDimensionChangeSplitsTheRun(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(200*time.Millisecond), "RUNNING"),
		state("ComputeInstance", "vm-7", at(2800*time.Millisecond), "STOPPED"),
		fact("ComputeInstance", "vm-7", "status.instance_type", at(200*time.Millisecond), "small"),
		fact("ComputeInstance", "vm-7", "status.instance_type", at(1500*time.Millisecond), "large"),
	}
	expectPieces(t, engine, facts, at(time.Minute), []string{
		"ComputeInstance compute 10:00:00.200000..10:00:01.500000 rate=1000000 micro=1300000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"instance_type\":\"small\"," +
			"\"meter_type\":\"second\",\"tenant\":\"acme\"}",
		"ComputeInstance compute 10:00:01.500000..10:00:02.800000 rate=1000000 micro=1300000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"instance_type\":\"large\"," +
			"\"meter_type\":\"second\",\"tenant\":\"acme\"}",
	})
}

func TestMidnightSplitsThePiece(t *testing.T) {
	engine := defaultEngine(t)
	midnight := time.Date(2026, 10, 6, 0, 0, 0, 0, time.UTC)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", midnight.Add(-time.Minute)),
		state("ComputeInstance", "vm-7", midnight.Add(-time.Minute), "RUNNING"),
	}
	expectPieces(t, engine, facts, midnight.Add(time.Minute), []string{
		"ComputeInstance compute 23:59:00.000000..00:00:00.000000 rate=1000000 micro=60000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
		"ComputeInstance compute 00:00:00.000000..00:01:00.000000 rate=1000000 micro=60000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}

func TestNothingBillsAfterDeletion(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(0), "RUNNING"),
		// A late state stamp after the deletion must not extend billing.
		state("ComputeInstance", "vm-7", at(2*time.Minute), "RUNNING"),
		fact("ComputeInstance", "vm-7", "deleted", at(time.Minute), "true"),
	}
	expectPieces(t, engine, facts, at(5*time.Minute), []string{
		"ComputeInstance compute 10:00:00.000000..10:01:00.000000 rate=1000000 micro=60000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}

func TestClusterBillsInstallationRetroactivelyOnReady(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("Cluster", "c-1", at(0)),
		state("Cluster", "c-1", at(0), "PROGRESSING"),
		state("Cluster", "c-1", at(5*time.Minute), "READY"),
		fact("Cluster", "c-1", "spec.version", at(0), "4.18"),
	}
	expectPieces(t, engine, facts, at(10*time.Minute), []string{
		"Cluster control_plane 10:00:00.000000..10:10:00.000000 rate=1000000 micro=600000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\",\"version\":\"4.18\"}",
	})
}

func TestClusterThatNeverReachesReadyBillsNothing(t *testing.T) {
	engine := defaultEngine(t)
	for _, terminal := range []string{"FAILED", "DELETING"} {
		facts := []database.Fact{
			created("Cluster", "c-1", at(0)),
			state("Cluster", "c-1", at(0), "PROGRESSING"),
			state("Cluster", "c-1", at(5*time.Minute), terminal),
		}
		expectPieces(t, engine, facts, at(10*time.Minute), nil)
	}
	// Deletion during provisioning also bills nothing.
	facts := []database.Fact{
		created("Cluster", "c-1", at(0)),
		state("Cluster", "c-1", at(0), "PROGRESSING"),
		fact("Cluster", "c-1", "deleted", at(2*time.Minute), "true"),
	}
	expectPieces(t, engine, facts, at(10*time.Minute), nil)
}

func TestClusterBillsAgainAfterFailure(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("Cluster", "c-1", at(0)),
		state("Cluster", "c-1", at(0), "PROGRESSING"),
		state("Cluster", "c-1", at(time.Minute), "FAILED"),
		state("Cluster", "c-1", at(2*time.Minute), "PROGRESSING"),
		state("Cluster", "c-1", at(3*time.Minute), "READY"),
	}
	expectPieces(t, engine, facts, at(4*time.Minute), []string{
		// The first PROGRESSING minute is never billed; the second span is, retroactively.
		"Cluster control_plane 10:02:00.000000..10:04:00.000000 rate=1000000 micro=120000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}

func TestBareMetalAllocationAndConsumption(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("BareMetalInstance", "bm-1", at(0)),
		state("BareMetalInstance", "bm-1", at(0), "PROVISIONING"),
		state("BareMetalInstance", "bm-1", at(time.Minute), "STARTING"),
		state("BareMetalInstance", "bm-1", at(2*time.Minute), "RUNNING"),
		state("BareMetalInstance", "bm-1", at(3*time.Minute), "STOPPED"),
		state("BareMetalInstance", "bm-1", at(4*time.Minute), "DELETING"),
		fact("BareMetalInstance", "bm-1", "spec.instance_type", at(0), "m7a"),
	}
	expectPieces(t, engine, facts, at(5*time.Minute), []string{
		"BareMetalInstance allocation 10:01:00.000000..10:05:00.000000 rate=1000000 " +
			"micro=240000000 pending=false dims={\"deployment\":\"us-east\"," +
			"\"instance_type\":\"m7a\",\"meter_type\":\"second\",\"tenant\":\"acme\"}",
		"BareMetalInstance consumption 10:02:00.000000..10:03:00.000000 rate=1000000 " +
			"micro=60000000 pending=false dims={\"deployment\":\"us-east\"," +
			"\"instance_type\":\"m7a\",\"meter_type\":\"second\",\"tenant\":\"acme\"}",
	})
}

func TestVolumeCapacityMultipliesByProvisionedGib(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("Volume", "vol-1", at(0)),
		state("Volume", "vol-1", at(0), "CREATING"),
		state("Volume", "vol-1", at(time.Minute), "AVAILABLE"),
		state("Volume", "vol-1", at(3*time.Minute), "DELETING"),
		fact("Volume", "vol-1", "status.vendor_volume_id", at(0), "vend-9"),
		fact("Volume", "vol-1", "status.protocol", at(0), "BLOCK"),
		fact("Volume", "vol-1", "status.provisioned_size_gib", at(0), "100"),
		fact("Volume", "vol-1", "spec.storage_tier", at(0), "gold"),
	}
	expectPieces(t, engine, facts, at(4*time.Minute), []string{
		"Volume capacity 10:01:00.000000..10:04:00.000000 rate=100000000 micro=18000000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"gibibyte_second\"," +
			"\"storage_tier\":\"gold\",\"tenant\":\"acme\"}",
	})
}

func TestVolumeWithoutVendorIDOrBlockProtocolBillsNothing(t *testing.T) {
	engine := defaultEngine(t)
	base := []database.Fact{
		created("Volume", "vol-1", at(0)),
		state("Volume", "vol-1", at(time.Minute), "AVAILABLE"),
		fact("Volume", "vol-1", "status.provisioned_size_gib", at(0), "100"),
	}
	noVendor := append(append([]database.Fact{}, base...),
		fact("Volume", "vol-1", "status.protocol", at(0), "BLOCK"))
	expectPieces(t, engine, noVendor, at(3*time.Minute), nil)
	nfs := append(append([]database.Fact{}, base...),
		fact("Volume", "vol-1", "status.vendor_volume_id", at(0), "vend-9"),
		fact("Volume", "vol-1", "status.protocol", at(0), "NFS"))
	expectPieces(t, engine, nfs, at(3*time.Minute), nil)
}

func TestVolumeDeletedBeforeAvailableBillsNothing(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("Volume", "vol-1", at(0)),
		state("Volume", "vol-1", at(0), "PENDING"),
		state("Volume", "vol-1", at(time.Minute), "DELETING"),
		fact("Volume", "vol-1", "status.vendor_volume_id", at(0), "vend-9"),
		fact("Volume", "vol-1", "status.protocol", at(0), "BLOCK"),
		fact("Volume", "vol-1", "status.provisioned_size_gib", at(0), "100"),
	}
	expectPieces(t, engine, facts, at(3*time.Minute), nil)
}

func TestExternalIPAllocationDimensions(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ExternalIP", "ip-1", at(0)),
		state("ExternalIP", "ip-1", at(0), "PENDING"),
		state("ExternalIP", "ip-1", at(time.Minute), "ALLOCATED"),
		fact("ExternalIP", "ip-1", "spec.pool", at(0), "pool-a"),
		// The tracked address, from which the family derives.
		fact("ExternalIP", "ip-1", "status.address", at(time.Minute), "2001:db8::1"),
		fact("ExternalIP", "ip-1", "status.attached", at(time.Minute), "true"),
		fact("ExternalIP", "ip-1", "status.attribution", at(time.Minute), "compute_instance/vm-7/API"),
	}
	expectPieces(t, engine, facts, at(2*time.Minute), []string{
		"ExternalIP allocation 10:01:00.000000..10:02:00.000000 rate=1000000 micro=60000000 " +
			"pending=false dims={\"attached\":\"true\",\"attribution\":" +
			"\"compute_instance/vm-7/API\",\"deployment\":\"us-east\",\"ip_family\":\"ipv6\"," +
			"\"meter_type\":\"second\",\"pool\":\"pool-a\",\"tenant\":\"acme\"}",
	})
}

func TestNATGatewayAllocation(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("NATGateway", "nat-1", at(0)),
		state("NATGateway", "nat-1", at(0), "PENDING"),
		state("NATGateway", "nat-1", at(time.Minute), "READY"),
		state("NATGateway", "nat-1", at(2*time.Minute), "DELETING"),
		fact("NATGateway", "nat-1", "spec.virtual_network", at(0), "vn-1"),
		fact("NATGateway", "nat-1", "spec.external_ip", at(0), "ip-1"),
	}
	expectPieces(t, engine, facts, at(3*time.Minute), []string{
		"NATGateway allocation 10:01:00.000000..10:03:00.000000 rate=1000000 micro=120000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"external_ip\":\"ip-1\"," +
			"\"meter_type\":\"second\",\"tenant\":\"acme\",\"virtual_network\":\"vn-1\"}",
	})
}

// TestOnceReachedWhenRunStartsInOnceState checks that a run opened by a condition change,
// not a state change, bills when the resource was already in a once state: a meter whose
// condition is a changing field.
func TestOnceReachedWhenRunStartsInOnceState(t *testing.T) {
	meters := `- resource: ExternalIP
  meter: attached_allocation
  unit: second
  billable_when:
    states: [ALLOCATED]
    once: [ALLOCATED]
    fields: {attached: "true"}
  dimensions: [pool]
`
	revision, err := rules.Parse(
		rules.NamePrefix+"0001", time.Unix(0, 0).UTC().Format(time.RFC3339), meters,
	)
	if err != nil {
		t.Fatalf("parsing revision: %v", err)
	}
	engine, err := usage.NewEngine([]rules.Revision{revision}, "us-east")
	if err != nil {
		t.Fatalf("creating engine: %v", err)
	}

	facts := []database.Fact{
		created("ExternalIP", "ip-1", at(0)),
		state("ExternalIP", "ip-1", at(0), "ALLOCATED"),
		fact("ExternalIP", "ip-1", "status.attached", at(0), "false"),
		// The attachment turns the meter billable at 10:02, while the resource has
		// been ALLOCATED since 10:00.
		fact("ExternalIP", "ip-1", "status.attached", at(2*time.Minute), "true"),
		fact("ExternalIP", "ip-1", "spec.pool", at(0), "pool-a"),
	}
	expectPieces(t, engine, facts, at(3*time.Minute), []string{
		"ExternalIP attached_allocation 10:02:00.000000..10:03:00.000000 rate=1000000 " +
			"micro=60000000 pending=false dims={\"deployment\":\"us-east\"," +
			"\"meter_type\":\"second\",\"pool\":\"pool-a\",\"tenant\":\"acme\"}",
	})
}

// TestQuantityMicroRoundsToNearestMicro checks the rounding policy: a fractional multiplier
// must not re-bill its rounding remainder every round.
func TestQuantityMicroRoundsToNearestMicro(t *testing.T) {
	piece := usage.Piece{
		StartTime: at(0),
		EndTime:   at(1500 * time.Millisecond),
		// A rate of one micro unit per second over 1.5 s is 1.5 micro units.
		RateMicro: 1,
	}
	if got := piece.QuantityMicro(); got != 2 {
		t.Errorf("1.5 micro units must round to 2, got %d", got)
	}
	piece.EndTime = at(1400 * time.Millisecond)
	if got := piece.QuantityMicro(); got != 1 {
		t.Errorf("1.4 micro units must round to 1, got %d", got)
	}
	piece.EndTime = at(500 * time.Millisecond)
	if got := piece.QuantityMicro(); got != 1 {
		t.Errorf("0.5 micro units must round to 1, got %d", got)
	}
	piece.RateMicro = 1_000_000
	piece.EndTime = at(time.Second)
	if got := piece.QuantityMicro(); got != 1_000_000 {
		t.Errorf("60 whole micro units must stay exact, got %d", got)
	}
}

// TestEveryMeterSendsTheCatalogDimension checks the dimension name the design specifies.
func TestEveryMeterSendsTheCatalogDimension(t *testing.T) {
	engine := defaultEngine(t)
	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(0), "RUNNING"),
		fact("ComputeInstance", "vm-7", "spec.catalog_item", at(0), "ci-rhel10-gpu"),
	}
	expectPieces(t, engine, facts, at(time.Minute), []string{
		"ComputeInstance compute 10:00:00.000000..10:01:00.000000 rate=1000000 " +
			"micro=60000000 pending=false dims={\"catalog\":\"ci-rhel10-gpu\"," +
			"\"deployment\":\"us-east\",\"meter_type\":\"second\",\"tenant\":\"acme\"}",
	})
}

func TestRevisionChangeSplitsUsage(t *testing.T) {
	first, err := rules.DefaultRevision()
	if err != nil {
		t.Fatalf("parsing default revision: %v", err)
	}
	secondYAML := `- resource: ComputeInstance
  meter: compute
  unit: second
  billable_when:
    states: [RUNNING, PAUSED]
  dimensions: [instance_type]
`
	effective := at(2 * time.Minute)
	second, err := rules.Parse(
		rules.NamePrefix+"0002", effective.Format(time.RFC3339), secondYAML,
	)
	if err != nil {
		t.Fatalf("parsing second revision: %v", err)
	}
	engine, err := usage.NewEngine([]rules.Revision{first, second}, "us-east")
	if err != nil {
		t.Fatalf("creating engine: %v", err)
	}

	facts := []database.Fact{
		created("ComputeInstance", "vm-7", at(0)),
		state("ComputeInstance", "vm-7", at(0), "RUNNING"),
		state("ComputeInstance", "vm-7", at(3*time.Minute), "PAUSED"),
	}
	expectPieces(t, engine, facts, at(4*time.Minute), []string{
		// RUNNING bills under both revisions, PAUSED only under the second.
		"ComputeInstance compute 10:00:00.000000..10:02:00.000000 rate=1000000 micro=120000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
		"ComputeInstance compute 10:02:00.000000..10:04:00.000000 rate=1000000 micro=120000000 " +
			"pending=false dims={\"deployment\":\"us-east\",\"meter_type\":\"second\"," +
			"\"tenant\":\"acme\"}",
	})
}
