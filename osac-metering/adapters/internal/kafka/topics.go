/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package kafka

import (
	"fmt"
	"sort"
	"strings"

	"github.com/IBM/sarama"
)

// Each tenant owns a heartbeat topic and a dead-letter topic. The names match the ones
// fulfillment's tenant onboarding creates.
const (
	// HeartbeatPrefix is prepended to the tenant name to form the topic that carries the
	// tenant's metering heartbeats.
	HeartbeatPrefix = "osac.metering.heartbeat."

	// DeadLetterPrefix is prepended to the tenant name to form the topic that carries the
	// tenant's heartbeats rejected by the billing provider.
	DeadLetterPrefix = "osac.metering.dlq."
)

// HeartbeatTopics lists every tenant's heartbeat topic. Adapters discover tenants this way.
func HeartbeatTopics(client sarama.Client) ([]string, error) {
	names, err := client.Topics()
	if err != nil {
		return nil, fmt.Errorf("listing kafka topics: %w", err)
	}
	var topics []string
	for _, name := range names {
		if strings.HasPrefix(name, HeartbeatPrefix) {
			topics = append(topics, name)
		}
	}
	sort.Strings(topics)
	return topics, nil
}

// DeadLetterTopics lists every tenant's dead-letter topic.
func DeadLetterTopics(client sarama.Client) ([]string, error) {
	names, err := client.Topics()
	if err != nil {
		return nil, fmt.Errorf("listing kafka topics: %w", err)
	}
	var topics []string
	for _, name := range names {
		if strings.HasPrefix(name, DeadLetterPrefix) {
			topics = append(topics, name)
		}
	}
	sort.Strings(topics)
	return topics, nil
}

// DeadLetterTopicOf maps a heartbeat topic to the tenant's dead-letter topic. It returns the
// input unchanged when the topic is not a heartbeat topic.
func DeadLetterTopicOf(source string) string {
	if strings.HasPrefix(source, HeartbeatPrefix) {
		return DeadLetterPrefix + source[len(HeartbeatPrefix):]
	}
	return source
}
