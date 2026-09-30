/*
Copyright (c) 2026 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations under the License.
*/

package quota

import (
	"context"
	"log/slog"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2/dsl/core"
	. "github.com/onsi/gomega"

	"github.com/osac-project/osac/fulfillment-service/internal/database"
	"github.com/osac-project/osac/fulfillment-service/internal/logging"
)

func TestQuota(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Quota package")
}

var (
	quotaLogger   *slog.Logger
	quotaDatabase *database.Container
)

var _ = BeforeSuite(func(ctx context.Context) {
	var err error
	quotaLogger, err = logging.NewLogger().
		SetLevel(slog.LevelDebug.String()).
		SetOut(GinkgoWriter).
		Build()
	Expect(err).ToNot(HaveOccurred())

	quotaDatabase, err = database.NewContainer().SetLogger(quotaLogger).Build()
	Expect(err).ToNot(HaveOccurred())
	startCtx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	Expect(quotaDatabase.Start(startCtx)).To(Succeed())
	DeferCleanup(func() {
		stopCtx, stopCancel := context.WithTimeout(context.Background(), time.Minute)
		defer stopCancel()
		Expect(quotaDatabase.Stop(stopCtx)).To(Succeed())
	})
})
