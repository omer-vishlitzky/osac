{{- define "osac-metering.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "osac-metering.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "osac-metering.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "osac-metering.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "osac-metering.selectorLabels" -}}
app.kubernetes.io/name: {{ include "osac-metering.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "osac-metering.kafkaClusterName" -}}
{{- .Values.kafka.clusterName | default "osac-kafka" }}
{{- end -}}

{{- define "osac-metering.kafkaClusterNamespace" -}}
{{- .Values.kafka.clusterNamespace | default "osac-kafka" }}
{{- end -}}

{{- define "osac-metering.kafkaBrokers" -}}
{{- .Values.kafka.brokers | default "osac-kafka-kafka-bootstrap.osac-kafka.svc.cluster.local:9093" }}
{{- end -}}

{{- define "osac-metering.kafkaCaSecret" -}}
{{- .Values.kafka.caSecret | default "osac-kafka-cluster-ca-cert" }}
{{- end -}}

{{/*
Topic prefix: when set, prepended to all topic names with a dot separator.
*/}}
{{- define "osac-metering.topicPrefix" -}}
{{- .Values.topicPrefix | default "" }}
{{- end -}}

{{- define "osac-metering.kafkaTopic" -}}
{{- $prefix := include "osac-metering.topicPrefix" . -}}
{{- if $prefix }}{{ $prefix }}.{{ end }}osac.metering.lifecycle
{{- end -}}

{{- define "osac-metering.kafkaSaslUsername" -}}
{{- $prefix := include "osac-metering.topicPrefix" . -}}
{{- if $prefix }}{{ $prefix }}-{{ end }}osac-metering
{{- end -}}

{{- define "osac-metering.kafkaSaslSecretName" -}}
{{- $prefix := include "osac-metering.topicPrefix" . -}}
{{- if $prefix }}{{ $prefix }}-{{ end }}osac-metering
{{- end -}}

{{- define "osac-metering.kafkaReplicas" -}}
3
{{- end -}}
