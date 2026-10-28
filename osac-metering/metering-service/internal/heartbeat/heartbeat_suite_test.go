package heartbeat_test

import (
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/osac-project/osac-metering/internal/cireports"
)

func TestHeartbeat(t *testing.T) {
	RegisterFailHandler(Fail)
	cireports.RegisterNativeReportCollector()
	RunSpecs(t, "Heartbeat Suite")
}
