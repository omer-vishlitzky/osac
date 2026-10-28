package reconciliation_test

import (
	"testing"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	"github.com/osac-project/osac-metering/internal/cireports"
)

func TestReconciliation(t *testing.T) {
	RegisterFailHandler(Fail)
	cireports.RegisterNativeReportCollector()
	RunSpecs(t, "Reconciliation Suite")
}
