/*
Copyright (c) 2025 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package events

import "fmt"

// DimensionsEqual compares two billing dimension maps for equality.
// Values are compared by their string representation to handle type
// differences (e.g., int32 vs int) from JSON round-tripping.
func DimensionsEqual(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if fmt.Sprintf("%v", b[k]) != fmt.Sprintf("%v", v) {
			return false
		}
	}
	return true
}
