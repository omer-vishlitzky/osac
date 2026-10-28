package events_test

import (
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/osac-project/osac-metering/internal/cireports"
)

func TestEvents(t *testing.T) {
	RegisterFailHandler(Fail)
	cireports.RegisterNativeReportCollector()
	RunSpecs(t, "Events Suite")
}
