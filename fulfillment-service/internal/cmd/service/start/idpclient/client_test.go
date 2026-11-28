//
// Copyright (c) 2026 Red Hat, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
// specific language governing permissions and limitations under the License.
//

package idpclient

import (
	"io"
	"log/slog"
	"strings"
	"testing"
)

func TestRealmFromIssuer(t *testing.T) {
	for _, tc := range []struct {
		issuer string
		realm  string
		found  bool
	}{
		{"https://keycloak.example.com/realms/osac", "osac", true},
		{"https://keycloak.example.com/realms/demoav1", "demoav1", true},
		{"https://keycloak.example.com/realms/demoav1/", "demoav1", true},
		{"https://kc.example.com:8443/realms/run-42", "run-42", true},
		// A path prefix before the realms segment is still a realm segment.
		{"https://keycloak.example.com/auth/realms/osac", "osac", true},
		// Legacy shapes without a realms segment leave the realm unresolved.
		{"https://keycloak.example.com", "", false},
		{"", "", false},
		// A trailing realms segment with no name does not identify a realm.
		{"https://keycloak.example.com/realms/", "", false},
	} {
		realm, found := realmFromIssuer(tc.issuer)
		if realm != tc.realm || found != tc.found {
			t.Errorf("realmFromIssuer(%q) = (%q, %v), want (%q, %v)",
				tc.issuer, realm, found, tc.realm, tc.found)
		}
	}
}

func TestResolveRealmPrecedence(t *testing.T) {
	for _, tc := range []struct {
		name   string
		issuer string
		realm  string
		env    string
		want   string
	}{
		{
			name:   "explicit realm wins over the environment and the issuer",
			issuer: "https://keycloak.example.com/realms/osac",
			realm:  "demoav1",
			env:    "envrealm",
			want:   "demoav1",
		},
		{
			name:   "environment wins over the issuer",
			issuer: "https://keycloak.example.com/realms/osac",
			env:    "demoav1",
			want:   "demoav1",
		},
		{
			name:   "issuer decides when nothing is set",
			issuer: "https://keycloak.example.com/realms/demoav1",
			env:    "  ",
			want:   "demoav1",
		},
		{
			name:   "explicit realm resolves a legacy issuer",
			issuer: "https://keycloak.example.com",
			realm:  "demoav1",
			want:   "demoav1",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(realmEnvVar, tc.env)
			builder := &ClientBuilder{issuerURL: tc.issuer, realm: tc.realm}
			got, err := builder.resolveRealm()
			if err != nil {
				t.Fatalf("resolveRealm() returned error: %v", err)
			}
			if got != tc.want {
				t.Errorf("resolveRealm() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestResolveRealmRejectsUndeterminableRealm(t *testing.T) {
	t.Setenv(realmEnvVar, "")
	builder := &ClientBuilder{issuerURL: "https://keycloak.example.com"}
	realm, err := builder.resolveRealm()
	if err == nil {
		t.Fatalf("resolveRealm() = %q, want an error for an issuer without a realm", realm)
	}
	if realm != "" {
		t.Errorf("resolveRealm() = %q, want no realm with the error", realm)
	}
}

func TestResolveRealmRejectsUnsafeRealm(t *testing.T) {
	for _, realm := range []string{"../osac", "osac/admin", "real ms", "osac?x=1"} {
		t.Run(realm, func(t *testing.T) {
			t.Setenv(realmEnvVar, "")
			builder := &ClientBuilder{issuerURL: "https://keycloak.example.com/realms/osac", realm: realm}
			if got, err := builder.resolveRealm(); err == nil {
				t.Errorf("resolveRealm() = %q, want an error for realm %q", got, realm)
			}
		})
	}
}

func TestBuildRequiresADeterminableRealm(t *testing.T) {
	t.Setenv(realmEnvVar, "")
	_, err := NewClient().
		SetLogger(slog.New(slog.NewTextHandler(io.Discard, nil))).
		SetBaseURL("https://keycloak.example.com").
		SetIssuerURL("https://keycloak.example.com").
		Build()
	if err == nil {
		t.Fatal("Build() succeeded without a realm, want an error")
	}
	if !strings.Contains(err.Error(), realmEnvVar) {
		t.Errorf("Build() error = %q, want it to name %s", err, realmEnvVar)
	}
}
