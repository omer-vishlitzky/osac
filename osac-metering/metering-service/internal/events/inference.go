/*
Copyright (c) 2026 Red Hat, Inc.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
in compliance with the License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0
*/

package events

// InferenceUsageData is the CloudEvent data payload for MaaS inference events.
type InferenceUsageData struct {
	ResourceID        string                     `json:"resource_id"`
	ResourceType      string                     `json:"resource_type"`
	TenantID          string                     `json:"tenant_id"`
	BillingDimensions InferenceBillingDimensions `json:"billing_dimensions"`
	SchemaVersion     string                     `json:"schema_version"`
}

// InferenceBillingDimensions carries MaaS-specific billing fields.
type InferenceBillingDimensions struct {
	OrganizationID      string  `json:"organization_id"`
	CostCenter          string  `json:"cost_center,omitempty"`
	Subscription        string  `json:"subscription,omitempty"`
	Provider            string  `json:"provider,omitempty"`
	Model               string  `json:"model"`
	PromptTokens        int     `json:"prompt_tokens"`
	CompletionTokens    int     `json:"completion_tokens"`
	TotalTokens         int     `json:"total_tokens"`
	CachedInputTokens   int     `json:"cached_input_tokens"`
	CacheCreationTokens int     `json:"cache_creation_tokens"`
	ReasoningTokens     int     `json:"reasoning_tokens"`
	DurationMs          float64 `json:"duration_ms"`
}
