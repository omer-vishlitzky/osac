package provisioning_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	. "github.com/onsi/gomega"
	"go.uber.org/zap/zapcore"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	"github.com/osac-project/osac/osac-operator/api/v1alpha1"
	"github.com/osac-project/osac/osac-operator/pkg/aap"
	"github.com/osac-project/osac/osac-operator/pkg/provisioning"
)

// These tests pin the AAP boundary events: one info event per real launch and
// one V(2) event per status return, driven through the real aap.Client against
// an httptest AAP server with a real zap logger and real resource objects.

const (
	boundaryEventResourceUID   = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	boundaryEventResourceUUID  = "11111111-2222-4333-8444-555555555555"
	boundaryEventBadUUIDLabel  = "Bearer not-a-uuid-secret-like-junk"
	boundaryEventSecretMarker  = "boundary-event-secret-marker"
	boundaryEventAdminKubeconf = "apiVersion: v1\nclusters: []\n"
)

// boundaryEventLog is a zap JSON log captured in memory.
type boundaryEventLog struct {
	buffer *bytes.Buffer
}

func newBoundaryEventLog() *boundaryEventLog {
	return &boundaryEventLog{buffer: &bytes.Buffer{}}
}

// defaultContext returns a context whose logger matches the operator's default
// level: debug, so V(1) is enabled and V(2) is not.
func (l *boundaryEventLog) defaultContext() context.Context {
	logger := zap.New(zap.WriteTo(l.buffer), zap.Level(zapcore.DebugLevel))
	return ctrllog.IntoContext(context.Background(), logger)
}

// verboseContext returns a context whose logger also enables V(2).
func (l *boundaryEventLog) verboseContext() context.Context {
	logger := zap.New(zap.WriteTo(l.buffer), zap.Level(zapcore.Level(-2)))
	return ctrllog.IntoContext(context.Background(), logger)
}

// events returns the parsed log entries whose message matches. A malformed
// captured line fails the test: silently skipping it could hide a broken event.
func (l *boundaryEventLog) events(t *testing.T, message string) []map[string]any {
	t.Helper()
	matches := []map[string]any{}
	for _, line := range strings.Split(l.buffer.String(), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		entry := map[string]any{}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatalf("malformed captured log line %q: %v", line, err)
		}
		if entry["msg"] == message {
			matches = append(matches, entry)
		}
	}
	return matches
}

func (l *boundaryEventLog) contains(text string) bool {
	return strings.Contains(l.buffer.String(), text)
}

// boundaryFields returns the event's own fields: everything except the zap
// transport keys. Asserting on this set pins both presence and absence.
func boundaryFields(t *testing.T, event map[string]any) map[string]any {
	t.Helper()
	fields := map[string]any{}
	for key, value := range event {
		switch key {
		case "level", "ts", "msg", "caller", "logger":
		default:
			fields[key] = value
		}
	}
	return fields
}

func assertBoundaryIdentity(g *WithT, fields map[string]any) {
	g.Expect(fields).To(HaveKeyWithValue("kind", "ComputeInstance"))
	g.Expect(fields).To(HaveKeyWithValue("namespace", "boundary-tenant"))
	g.Expect(fields).To(HaveKeyWithValue("name", "boundary-resource"))
	g.Expect(fields).To(HaveKeyWithValue("uid", boundaryEventResourceUID))
	g.Expect(fields).To(HaveKeyWithValue("apiResourceUUID", boundaryEventResourceUUID))
}

func boundaryEventResource() *v1alpha1.ComputeInstance {
	instance := &v1alpha1.ComputeInstance{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "boundary-resource",
			Namespace: "boundary-tenant",
			UID:       types.UID(boundaryEventResourceUID),
			Labels: map[string]string{
				"osac.openshift.io/computeinstance-uuid": boundaryEventResourceUUID,
			},
		},
	}
	instance.SetGroupVersionKind(v1alpha1.GroupVersion.WithKind("ComputeInstance"))
	return instance
}

// encodeFixture writes a fixture response. The handler runs on the server's
// goroutine, so failures use t.Errorf: t.Fatalf must stay on the test goroutine.
func encodeFixture(t *testing.T, w http.ResponseWriter, body any) {
	t.Helper()
	if err := json.NewEncoder(w).Encode(body); err != nil {
		t.Errorf("encoding fixture response: %v", err)
	}
}

// boundaryEventServer serves the AAP endpoints the provider touches. The
// launch handler is injectable so failure and cancellation cases can control
// the launch response.
func boundaryEventServer(t *testing.T, launch func(w http.ResponseWriter, r *http.Request)) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v2/job_templates/"):
			// Template lookup by name: the job-template endpoint answers for
			// job template names and reports nothing for workflow names.
			name := r.URL.Query().Get("name")
			if name == "provision-workflow" || name == "deprovision-workflow" {
				encodeFixture(t, w, map[string]any{"count": 0, "results": []any{}})
				return
			}
			encodeFixture(t, w, map[string]any{
				"count":   1,
				"results": []any{map[string]any{"id": 7, "name": name}},
			})
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v2/workflow_job_templates/"):
			name := r.URL.Query().Get("name")
			encodeFixture(t, w, map[string]any{
				"count":   1,
				"results": []any{map[string]any{"id": 9, "name": name}},
			})
		case r.Method == http.MethodPost && (strings.HasPrefix(r.URL.Path, "/v2/job_templates/") ||
			strings.HasPrefix(r.URL.Path, "/v2/workflow_job_templates/")):
			launch(w, r)
		case r.Method == http.MethodGet && r.URL.Path == "/v2/jobs/":
			// ListJobs for a launch claim: one job whose extra_vars carry the claim.
			encodeFixture(t, w, map[string]any{
				"next": nil,
				"results": []any{map[string]any{
					"id":         876,
					"status":     "running",
					"extra_vars": `{"osac_job_launch_id":"boundary-claim-1"}`,
				}},
			})
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/v2/jobs/"):
			boundaryEventJob(t, w, r)
		default:
			t.Errorf("unexpected AAP request: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

// boundaryEventJob serves GetJob by job id: 31 pending, 32 running, 33 finished.
func boundaryEventJob(t *testing.T, w http.ResponseWriter, r *http.Request) {
	t.Helper()
	job := map[string]any{}
	switch strings.Trim(strings.TrimPrefix(r.URL.Path, "/v2/jobs/"), "/") {
	case "31":
		job = map[string]any{"id": 31, "status": "pending"}
	case "32":
		job = map[string]any{"id": 32, "status": "running", "started": "2026-10-28T10:00:00Z"}
	case "33":
		job = map[string]any{
			"id":       33,
			"status":   "successful",
			"started":  "2026-10-28T10:00:00Z",
			"finished": "2026-10-28T10:05:00Z",
		}
	default:
		t.Errorf("unexpected job id requested: %s", r.URL.Path)
	}
	encodeFixture(t, w, job)
}

func TestAAPLaunchBoundaryEventJobTemplateSuccess(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 4242})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.defaultContext()
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	result, err := provider.TriggerProvision(ctx, boundaryEventResource())
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(result.JobID).To(Equal("4242"))

	events := logs.events(t, "AAP job launched")
	g.Expect(events).To(HaveLen(1))
	g.Expect(events[0]["level"]).To(Equal("info"))
	fields := boundaryFields(t, events[0])
	assertBoundaryIdentity(g, fields)
	g.Expect(fields).To(HaveKeyWithValue("action", "create"))
	g.Expect(fields).To(HaveKeyWithValue("templateType", "job_template"))
	g.Expect(fields).To(HaveKeyWithValue("jobID", "4242"))
	g.Expect(fields).To(HaveKey("launchDurationMs"))
	g.Expect(fields["launchDurationMs"]).To(BeNumerically(">=", 0))
	g.Expect(fields).To(HaveLen(9), "unexpected extra fields: %v", fields)
}

func TestAAPLaunchBoundaryEventWorkflowTemplateSuccess(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 5353})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.defaultContext()
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-workflow", "deprovision-workflow")

	result, err := provider.TriggerProvision(ctx, boundaryEventResource())
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(result.JobID).To(Equal("5353"))

	events := logs.events(t, "AAP job launched")
	g.Expect(events).To(HaveLen(1))
	fields := boundaryFields(t, events[0])
	g.Expect(fields).To(HaveKeyWithValue("templateType", "workflow_job_template"))
	g.Expect(fields).To(HaveKeyWithValue("jobID", "5353"))
	g.Expect(fields).To(HaveKeyWithValue("action", "create"))
}

func TestAAPLaunchBoundaryEventFailureCarriesClassNotErrorText(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		if _, err := fmt.Fprintf(w, `{"detail":"internal error %s"}`, boundaryEventSecretMarker); err != nil {
			t.Errorf("writing launch failure fixture: %v", err)
		}
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.defaultContext()
	// The admin kubeconfig makes the launch sensitive: the AAP client then
	// keeps response bodies out of its own logs and errors, which is the
	// credential-bearing launch path this event must be safe for.
	ctx = provisioning.WithAdminKubeconfig(ctx, boundaryEventAdminKubeconf)
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	_, err := provider.TriggerProvision(ctx, boundaryEventResource())
	g.Expect(err).To(HaveOccurred())
	g.Expect(err.Error()).To(ContainSubstring("failed to launch job template"))

	events := logs.events(t, "AAP job launch failed")
	g.Expect(events).To(HaveLen(1))
	g.Expect(events[0]["level"]).To(Equal("info"))
	fields := boundaryFields(t, events[0])
	assertBoundaryIdentity(g, fields)
	g.Expect(fields).To(HaveKeyWithValue("action", "create"))
	g.Expect(fields).To(HaveKeyWithValue("templateType", "job_template"))
	g.Expect(fields).To(HaveKeyWithValue("result", "error"))
	g.Expect(fields).To(HaveKey("launchDurationMs"))
	g.Expect(fields).To(HaveLen(9), "unexpected extra fields: %v", fields)

	// The failure event and the whole log carry neither the error text nor the
	// secret-bearing response body nor the admin kubeconfig.
	eventJSON, marshalErr := json.Marshal(events[0])
	g.Expect(marshalErr).NotTo(HaveOccurred())
	g.Expect(string(eventJSON)).NotTo(ContainSubstring("failed to launch"))
	g.Expect(string(eventJSON)).NotTo(ContainSubstring(boundaryEventSecretMarker))
	g.Expect(logs.contains(boundaryEventSecretMarker)).To(BeFalse())
	g.Expect(logs.contains(boundaryEventAdminKubeconf)).To(BeFalse())
}

func TestAAPLaunchBoundaryEventClassifiesCancelledRequest(t *testing.T) {
	g := NewWithT(t)
	launchCtx, cancel := context.WithCancel(context.Background())
	release := make(chan struct{})
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		// Cancel while the launch request is in flight, then hold the
		// response until the test releases it, so the launch error is the
		// context cancellation and not a response.
		cancel()
		<-release
	})
	defer server.Close()
	defer close(release)

	logs := newBoundaryEventLog()
	ctx := ctrllog.IntoContext(launchCtx, zap.New(zap.WriteTo(logs.buffer), zap.Level(zapcore.DebugLevel)))
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	_, err := provider.TriggerProvision(ctx, boundaryEventResource())
	g.Expect(err).To(HaveOccurred())
	g.Expect(err.Error()).To(ContainSubstring("failed to launch job template"))

	events := logs.events(t, "AAP job launch failed")
	g.Expect(events).To(HaveLen(1))
	fields := boundaryFields(t, events[0])
	g.Expect(fields).To(HaveKeyWithValue("result", "cancelled"))
	g.Expect(fields).To(HaveKey("launchDurationMs"))
}

func TestAAPLaunchBoundaryEventReusesLaunchClaimJob(t *testing.T) {
	g := NewWithT(t)
	launchCalls := 0
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		launchCalls++
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 999})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.defaultContext()
	ctx = provisioning.WithAAPJobLaunchID(ctx, "boundary-claim-1")
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	result, err := provider.TriggerDeprovision(ctx, boundaryEventResource(), nil)
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(result.Action).To(Equal(provisioning.DeprovisionTriggered))
	g.Expect(result.JobID).To(Equal("876"))
	g.Expect(launchCalls).To(BeZero(), "claim reuse must not launch a new job")

	events := logs.events(t, "AAP job launch reused existing job")
	g.Expect(events).To(HaveLen(1))
	fields := boundaryFields(t, events[0])
	assertBoundaryIdentity(g, fields)
	g.Expect(fields).To(HaveKeyWithValue("action", "delete"))
	g.Expect(fields).To(HaveKeyWithValue("templateType", "job_template"))
	g.Expect(fields).To(HaveKeyWithValue("jobID", "876"))
	// No request was made and the launch claim is not needed for correlation,
	// so no duration and no launch ID may appear.
	g.Expect(fields).NotTo(HaveKey("launchDurationMs"))
	g.Expect(fields).NotTo(HaveKey("launchID"))
}

func TestAAPStatusBoundaryEventReturnsNativeStateAndTimes(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 1})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.verboseContext()
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")
	resource := boundaryEventResource()

	status, err := provider.GetProvisionStatus(ctx, resource, "31")
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(status.State).To(Equal(v1alpha1.JobStatePending))

	events := logs.events(t, "AAP job status returned")
	g.Expect(events).To(HaveLen(1))
	g.Expect(events[0]["level"]).NotTo(Equal("info"), "status events must never sit at info level")
	fields := boundaryFields(t, events[0])
	assertBoundaryIdentity(g, fields)
	g.Expect(fields).To(HaveKeyWithValue("action", "create"))
	g.Expect(fields).To(HaveKeyWithValue("jobID", "31"))
	g.Expect(fields).To(HaveKeyWithValue("state", "pending"))
	// Unknown start and finish stay absent, never zero times.
	g.Expect(fields).NotTo(HaveKey("started"))
	g.Expect(fields).NotTo(HaveKey("finished"))
	g.Expect(fields).To(HaveLen(8), "unexpected extra fields: %v", fields)

	// A running job reports its native start time and no finish time.
	status, err = provider.GetProvisionStatus(ctx, resource, "32")
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(status.State).To(Equal(v1alpha1.JobStateRunning))
	running := boundaryFields(t, logs.events(t, "AAP job status returned")[1])
	g.Expect(running).To(HaveKeyWithValue("state", "running"))
	g.Expect(running).To(HaveKey("started"))
	started, parseErr := time.Parse(time.RFC3339, running["started"].(string))
	g.Expect(parseErr).NotTo(HaveOccurred())
	g.Expect(started.Equal(time.Date(2026, 10, 28, 10, 0, 0, 0, time.UTC))).To(BeTrue())
	g.Expect(running).NotTo(HaveKey("finished"))

	// A finished job reports both native times; this call is a deprovision
	// status, so the action field follows the calling boundary.
	status, err = provider.GetDeprovisionStatus(ctx, resource, "33")
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(status.State).To(Equal(v1alpha1.JobStateSucceeded))
	finished := boundaryFields(t, logs.events(t, "AAP job status returned")[2])
	g.Expect(finished).To(HaveKeyWithValue("state", "successful"))
	g.Expect(finished).To(HaveKeyWithValue("action", "delete"))
	finishedAt, parseErr := time.Parse(time.RFC3339, finished["finished"].(string))
	g.Expect(parseErr).NotTo(HaveOccurred())
	g.Expect(finishedAt.Equal(time.Date(2026, 10, 28, 10, 5, 0, 0, time.UTC))).To(BeTrue())

	// No event invented a queue or poll duration from local timestamps.
	for _, event := range logs.events(t, "AAP job status returned") {
		for key := range boundaryFields(t, event) {
			g.Expect(key).NotTo(ContainSubstring("duration"))
		}
	}
}

// The default logger runs at debug (V(1) enabled), so per-poll status detail at
// V(2) must not appear there; an explicitly verbose logger may enable it.
func TestAAPStatusBoundaryEventSuppressedAtDefaultDebugLevel(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 1})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.defaultContext()
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	status, err := provider.GetProvisionStatus(ctx, boundaryEventResource(), "31")
	g.Expect(err).NotTo(HaveOccurred())
	g.Expect(status.State).To(Equal(v1alpha1.JobStatePending), "status semantics must not depend on the log level")

	g.Expect(logs.events(t, "AAP job status returned")).To(BeEmpty())
	// V(1) logs still flow at debug, proving the logger is live and only the
	// V(2) status detail is filtered.
	g.Expect(logs.events(t, "AAP request starting")).NotTo(BeEmpty())
}

func TestAAPStatusBoundaryEventOmitsUnknownIdentityFields(t *testing.T) {
	g := NewWithT(t)
	server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		encodeFixture(t, w, map[string]any{"id": 1})
	})
	defer server.Close()

	logs := newBoundaryEventLog()
	ctx := logs.verboseContext()
	provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")

	// No GVK, no uid, no labels: those fields stay absent rather than guessed.
	instance := &v1alpha1.ComputeInstance{
		ObjectMeta: metav1.ObjectMeta{Name: "bare-resource", Namespace: "boundary-tenant"},
	}
	_, err := provider.GetProvisionStatus(ctx, instance, "31")
	g.Expect(err).NotTo(HaveOccurred())

	events := logs.events(t, "AAP job status returned")
	g.Expect(events).To(HaveLen(1))
	fields := boundaryFields(t, events[0])
	g.Expect(fields).NotTo(HaveKey("kind"))
	g.Expect(fields).NotTo(HaveKey("uid"))
	g.Expect(fields).NotTo(HaveKey("apiResourceUUID"))
	g.Expect(fields).To(HaveKeyWithValue("namespace", "boundary-tenant"))
	g.Expect(fields).To(HaveKeyWithValue("name", "bare-resource"))
}

// A malformed or secret-like value in the per-kind UUID label is skipped, not
// logged: the label is validated before it reaches an event.
func TestAAPBoundaryEventSkipsMalformedResourceUUID(t *testing.T) {
	for _, testCase := range []struct {
		name  string
		value string
	}{
		{name: "secret-like", value: boundaryEventBadUUIDLabel},
		{name: "compact", value: strings.ReplaceAll(boundaryEventResourceUUID, "-", "")},
		{name: "urn", value: "urn:uuid:" + boundaryEventResourceUUID},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			g := NewWithT(t)
			server := boundaryEventServer(t, func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
				encodeFixture(t, w, map[string]any{"id": 4242})
			})
			defer server.Close()

			logs := newBoundaryEventLog()
			ctx := logs.defaultContext()
			provider := provisioning.NewAAPProvider(aap.NewClient(server.URL, "token", false), "provision-job", "deprovision-job")
			instance := boundaryEventResource()
			instance.Labels["osac.openshift.io/computeinstance-uuid"] = testCase.value

			result, err := provider.TriggerProvision(ctx, instance)
			g.Expect(err).NotTo(HaveOccurred())
			g.Expect(result.JobID).To(Equal("4242"))

			events := logs.events(t, "AAP job launched")
			g.Expect(events).To(HaveLen(1))
			fields := boundaryFields(t, events[0])
			g.Expect(fields).To(HaveKeyWithValue("kind", "ComputeInstance"))
			g.Expect(fields).To(HaveKeyWithValue("uid", boundaryEventResourceUID))
			g.Expect(fields).NotTo(HaveKey("apiResourceUUID"))
			g.Expect(logs.contains(testCase.value)).To(BeFalse())
		})
	}
}
