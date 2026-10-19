/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package eventpublisher

import (
	"time"

	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/ginkgo/v2/dsl/table"
	. "github.com/onsi/gomega"

	"github.com/osac-project/osac/fulfillment-service/internal/servers"
)

var _ = DescribeTable("Topic retention flag values",
	func(text string, expected time.Duration) {
		value := &topicRetentionValue{}
		Expect(value.Set(text)).To(Succeed())
		Expect(value.retention).To(Equal(expected))
	},
	Entry("Kafka-style indefinite", "-1", servers.DefaultTopicRetention),
	Entry("Go duration", "168h", 168*time.Hour),
	Entry("Negative Go duration is indefinite", "-72h", -72*time.Hour),
)

var _ = It("Rejects invalid topic retention flag values", func() {
	value := &topicRetentionValue{}
	err := value.Set("forever")
	Expect(err).To(HaveOccurred())
	Expect(err.Error()).To(ContainSubstring("should be '-1' or a duration like '168h'"))
})

var _ = It("Shows indefinite retention in the Kafka style", func() {
	Expect((&topicRetentionValue{}).String()).To(Equal("0s"))
	Expect((&topicRetentionValue{retention: servers.DefaultTopicRetention}).String()).To(Equal("-1"))
	Expect((&topicRetentionValue{retention: 168 * time.Hour}).String()).To(Equal("168h0m0s"))
})
