package watch_test

import (
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/osac-project/osac-metering/internal/cireports"
)

func TestWatch(t *testing.T) {
	RegisterFailHandler(Fail)
	cireports.RegisterNativeReportCollector()
	RunSpecs(t, "Watch Suite")
}
