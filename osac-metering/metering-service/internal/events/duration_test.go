package events_test

import (
	"testing"
	"time"

	"github.com/osac-project/osac-metering/internal/events"
)

func TestDurationSeconds(t *testing.T) {
	start := time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)
	activeSince := start
	lastHeartbeat := start.Add(30 * time.Minute)
	componentSince := start.Add(45 * time.Minute)

	for _, test := range []struct {
		name            string
		end             time.Time
		lastHeartbeatAt *time.Time
		activeSince     *time.Time
		want            *float64
	}{
		{
			name:        "first interval starts at the active start",
			end:         start.Add(time.Hour),
			activeSince: &activeSince,
			want:        floatPointer(3600),
		},
		{
			name:            "later interval starts at the last heartbeat",
			end:             start.Add(time.Hour),
			lastHeartbeatAt: &lastHeartbeat,
			activeSince:     &activeSince,
			want:            floatPointer(1800),
		},
		{
			name:            "component active start clamps a resource heartbeat",
			end:             start.Add(time.Hour),
			lastHeartbeatAt: &lastHeartbeat,
			activeSince:     &componentSince,
			want:            floatPointer(900),
		},
		{
			name:            "last heartbeat can provide the lower bound",
			end:             start.Add(time.Hour),
			lastHeartbeatAt: &lastHeartbeat,
			want:            floatPointer(1800),
		},
		{
			name: "duration is unknown without either lower bound",
			end:  start.Add(time.Hour),
		},
		{
			name:            "end before lower bound is zero",
			end:             start.Add(15 * time.Minute),
			lastHeartbeatAt: &lastHeartbeat,
			activeSince:     &activeSince,
			want:            floatPointer(0),
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			got := events.DurationSeconds(test.end, test.lastHeartbeatAt, test.activeSince)
			if test.want == nil {
				if got != nil {
					t.Fatalf("DurationSeconds() = %v, want nil", *got)
				}
				return
			}
			if got == nil || *got != *test.want {
				t.Fatalf("DurationSeconds() = %v, want %v", got, *test.want)
			}
		})
	}
}

func floatPointer(value float64) *float64 {
	return &value
}
