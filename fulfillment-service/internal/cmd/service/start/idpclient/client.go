//
// Copyright (c) 2026 Red Hat, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

package idpclient

import (
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/osac-project/osac/fulfillment-service/internal/auth"
	"github.com/osac-project/osac/fulfillment-service/internal/idp"
	"github.com/osac-project/osac/fulfillment-service/internal/oauth"
	"github.com/osac-project/osac/fulfillment-service/internal/trust"
)

// ClientBuilder configures a Keycloak management client.
type ClientBuilder struct {
	logger           *slog.Logger
	baseURL          string
	issuerURL        string
	realm            string
	clientID         string
	clientIDFile     string
	clientSecret     string
	clientSecretFile string
	caPool           *trust.CertPool
}

// realmEnvVar overrides the realm derived from the issuer URL. It exists for a
// deployment whose issuer URL does not end in a realm segment; the start command
// does not pass a realm flag yet, so the variable is set on the container.
const realmEnvVar = "OSAC_IDP_REALM"

// NewClient creates a builder for a Keycloak management client.
func NewClient() *ClientBuilder {
	return &ClientBuilder{}
}

func (b *ClientBuilder) SetLogger(value *slog.Logger) *ClientBuilder {
	b.logger = value
	return b
}

func (b *ClientBuilder) SetBaseURL(value string) *ClientBuilder {
	b.baseURL = value
	return b
}

func (b *ClientBuilder) SetIssuerURL(value string) *ClientBuilder {
	b.issuerURL = value
	return b
}

// SetRealm sets the Keycloak realm the management client targets. It overrides
// the OSAC_IDP_REALM environment variable and the realm derived from the issuer
// URL, and is mandatory when the issuer URL carries no realm segment.
func (b *ClientBuilder) SetRealm(value string) *ClientBuilder {
	b.realm = value
	return b
}

func (b *ClientBuilder) SetClientID(value string) *ClientBuilder {
	b.clientID = value
	return b
}

func (b *ClientBuilder) SetClientIDFile(value string) *ClientBuilder {
	b.clientIDFile = value
	return b
}

func (b *ClientBuilder) SetClientSecret(value string) *ClientBuilder {
	b.clientSecret = value
	return b
}

func (b *ClientBuilder) SetClientSecretFile(value string) *ClientBuilder {
	b.clientSecretFile = value
	return b
}

func (b *ClientBuilder) SetCaPool(value *trust.CertPool) *ClientBuilder {
	b.caPool = value
	return b
}

// Build creates an IDP client using the configured service-account credentials.
func (b *ClientBuilder) Build() (*idp.Client, error) {
	if b.logger == nil {
		return nil, errors.New("logger is mandatory")
	}
	if b.baseURL == "" {
		return nil, errors.New("flag '--idp-url' is required")
	}
	if b.issuerURL == "" {
		return nil, errors.New("flag '--auth-issuer-url' is required")
	}
	realm, err := b.resolveRealm()
	if err != nil {
		return nil, err
	}
	clientID, err := credential("idp-client-id", b.clientID, b.clientIDFile)
	if err != nil {
		return nil, err
	}
	clientSecret, err := credential("idp-client-secret", b.clientSecret, b.clientSecretFile)
	if err != nil {
		return nil, err
	}
	store, err := auth.NewMemoryTokenStore().SetLogger(b.logger).Build()
	if err != nil {
		return nil, fmt.Errorf("failed to create IDP token store: %w", err)
	}
	source, err := oauth.NewTokenSource().SetLogger(b.logger).SetStore(store).SetCaPool(b.caPool).
		SetIssuer(b.issuerURL).SetFlow(oauth.CredentialsFlow).SetClientId(clientID).SetClientSecret(clientSecret).Build()
	if err != nil {
		return nil, fmt.Errorf("failed to create IDP token source: %w", err)
	}

	b.logger.Info("Creating Keycloak IDP client", slog.String("realm", realm))
	client, err := idp.NewClient().SetLogger(b.logger).SetBaseURL(b.baseURL).
		SetTokenSource(source).SetCaPool(b.caPool).
		SetRealmName(realm).Build()
	if err != nil {
		return nil, fmt.Errorf("failed to create Keycloak client: %w", err)
	}
	return client, nil
}

// resolveRealm returns the realm the management client targets: an explicit
// realm wins, then the OSAC_IDP_REALM environment variable, then the realm
// segment of the issuer URL. An issuer URL without that segment is an error
// rather than a silent fall back to the historical default, because the admin
// client must target the realm that issues its tokens and a mismatch is only
// visible later as a 403 on every call.
func (b *ClientBuilder) resolveRealm() (string, error) {
	realm := b.realm
	if realm == "" {
		realm = strings.TrimSpace(os.Getenv(realmEnvVar))
	}
	if realm == "" {
		derived, found := realmFromIssuer(b.issuerURL)
		if !found {
			return "", fmt.Errorf(
				"cannot derive the Keycloak realm from issuer URL %q; set environment "+
					"variable %q to the realm name",
				b.issuerURL, realmEnvVar,
			)
		}
		realm = derived
	}
	// The realm reaches admin request paths, some of which interpolate it
	// unescaped, so only a single path-free realm name is accepted.
	if url.PathEscape(realm) != realm {
		return "", fmt.Errorf("invalid Keycloak realm %q: it must be a single realm name", realm)
	}
	return realm, nil
}

// realmFromIssuer extracts the Keycloak realm from an issuer URL such as
// https://keycloak.example.com/realms/demoav1. It reports false when the URL
// carries no realm segment, leaving the caller to ask for the realm explicitly.
func realmFromIssuer(issuerURL string) (string, bool) {
	_, value, ok := strings.Cut(issuerURL, "/realms/")
	if !ok {
		return "", false
	}
	value = strings.TrimSuffix(value, "/")
	if value == "" {
		return "", false
	}
	return value, true
}

func credential(flag, value, path string) (string, error) {
	if value != "" && path != "" {
		return "", fmt.Errorf("flags '--%s' and '--%s-file' are mutually exclusive", flag, flag)
	}
	if path != "" {
		data, err := os.ReadFile(filepath.Clean(path))
		if err != nil {
			return "", fmt.Errorf("failed to read '--%s-file': %w", flag, err)
		}
		value = strings.TrimSpace(string(data))
	}
	if value == "" {
		return "", fmt.Errorf("flag '--%s' or a nonempty '--%s-file' is required", flag, flag)
	}
	return value, nil
}
