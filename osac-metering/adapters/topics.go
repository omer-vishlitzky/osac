/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package adapters

import "os"

var topicPrefix = os.Getenv("KAFKA_TOPIC_PREFIX")

var (
	TopicLifecycle   = topicPrefix + "osac.metering.lifecycle"
	TopicHeartbeat   = topicPrefix + "osac.metering.heartbeat"
	TopicCorrections = topicPrefix + "osac.metering.corrections"
	TopicInference   = topicPrefix + "osac.metering.inference"
)

var AllTopics = []string{TopicLifecycle, TopicHeartbeat, TopicCorrections, TopicInference}
