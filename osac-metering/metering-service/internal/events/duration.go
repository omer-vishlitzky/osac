package events

import "time"

// DurationSeconds returns the interval ending at end, bounded below by the
// later of the last heartbeat and the active interval start. A missing active
// start is allowed when a prior heartbeat is available; with neither bound,
// the duration is unknown.
func DurationSeconds(end time.Time, lastHeartbeatAt, activeSince *time.Time) *float64 {
	start := activeSince
	if lastHeartbeatAt != nil && (start == nil || lastHeartbeatAt.After(*start)) {
		start = lastHeartbeatAt
	}
	if start == nil {
		return nil
	}

	seconds := end.Sub(*start).Seconds()
	if seconds < 0 {
		seconds = 0
	}
	return &seconds
}
