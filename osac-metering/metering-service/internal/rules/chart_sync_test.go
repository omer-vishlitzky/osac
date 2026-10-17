/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package rules_test

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"

	"github.com/osac-project/osac-metering/internal/rules"
)

// The default meters ship twice: in code as the fallback definition and in the chart as the
// ConfigMap installs get. They must never drift apart.
func TestDefaultMetersMatchTheChart(t *testing.T) {
	_, testFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate this test file")
	}
	chartPath := filepath.Join(
		filepath.Dir(testFile), "..", "..", "..", "charts", "osac-metering", "rules", "0001-meters.yaml",
	)
	chart, err := os.ReadFile(chartPath)
	if err != nil {
		t.Fatalf("reading the chart's default meters: %v", err)
	}

	revision, err := rules.DefaultRevision()
	if err != nil {
		t.Fatalf("parsing the default revision: %v", err)
	}
	parsed, err := rules.Parse(
		rules.NamePrefix+"0001", "1970-01-01T00:00:00Z", string(chart),
	)
	if err != nil {
		t.Fatalf("the chart's meters do not parse: %v", err)
	}
	if !sameMeters(parsed.Meters, revision.Meters) {
		t.Fatal("the chart's default meters differ from the ones in code")
	}
}

// sameMeters compares two meter definition lists by value.
func sameMeters(a, b []rules.Meter) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		left, right := a[i], b[i]
		left.Component, right.Component = "", ""
		if !reflect.DeepEqual(left, right) {
			return false
		}
	}
	return true
}
