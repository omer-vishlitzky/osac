/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package testing

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/onsi/ginkgo/v2"
	"github.com/onsi/ginkgo/v2/reporters"
)

// ReportDirEnv keeps local runs unchanged unless collection is requested.
const ReportDirEnv = "OSAC_CI_GINKGO_REPORT_DIR"

const reportTempPattern = ".ginkgo-report-*.json.partial"

// Error text and report content may contain credentials.
const reportCollectionFailed = "OSAC CI native report collection failed for this suite"

// RegisterNativeReportCollector records native reports without failing specs.
// Call it before RunSpecs so the reporting node joins the suite.
func RegisterNativeReportCollector() {
	dir := os.Getenv(ReportDirEnv)
	if dir == "" {
		return
	}
	ginkgo.ReportAfterSuite("OSAC native report collector", func(report ginkgo.Report) {
		collectNativeReport(dir, report)
	})
}

func collectNativeReport(dir string, report ginkgo.Report) {
	temp, err := os.CreateTemp(dir, reportTempPattern)
	if err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	tempName := temp.Name()
	if err := temp.Close(); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	if err := reporters.GenerateJSONReport(report, tempName); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
		return
	}
	if err := publishNativeReport(tempName, dir); err != nil {
		ginkgo.GinkgoWriter.Println(reportCollectionFailed)
	}
}

// Linking prevents overwrites. Failed cleanup leaves a visible partial.
func publishNativeReport(tempPath string, dir string) error {
	if err := os.Link(tempPath, filepath.Join(dir, finalReportName(tempPath))); err != nil {
		return err
	}
	return os.Remove(tempPath)
}

func finalReportName(tempPath string) string {
	name := strings.TrimPrefix(filepath.Base(tempPath), ".")
	return strings.TrimSuffix(name, ".partial")
}
