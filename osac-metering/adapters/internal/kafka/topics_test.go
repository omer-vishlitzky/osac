/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package kafka

import (
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

var _ = Describe("adapter topics", func() {
	It("names the per-tenant topics", func() {
		Expect(HeartbeatPrefix + "acme").To(Equal("osac.metering.heartbeat.acme"))
		Expect(DeadLetterPrefix + "acme").To(Equal("osac.metering.dlq.acme"))
	})

	It("maps a heartbeat topic to the tenant's dead-letter topic", func() {
		Expect(DeadLetterTopicOf("osac.metering.heartbeat.acme")).
			To(Equal("osac.metering.dlq.acme"))
		Expect(DeadLetterTopicOf("osac.events.acme")).
			To(Equal("osac.events.acme"))
	})
})
