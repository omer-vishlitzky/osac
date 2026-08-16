# OSAC Monorepo Makefile -- entity-based deployment API.
#
# Entities:  Infra (osac-deps + osac-infra), Osac (osac chart), Test
# Parameters are REQUIRED -- missing params produce hard errors.
#
# Usage:
#   make install         PLATFORM=kind|openshift  PROFILE=dev|vmaas-ci|bmaas-ci|caas-ci|full-ci  NS=osac
#   make uninstall       PLATFORM=kind|openshift  PROFILE=dev|vmaas-ci|bmaas-ci|caas-ci|full-ci  NS=osac
#   make install-infra   PLATFORM=kind|openshift  PROFILE=dev|vmaas-ci|bmaas-ci|caas-ci|full-ci
#   make install-osac    PROFILE=dev|vmaas-ci|bmaas-ci|caas-ci|full-ci  NS=osac  [VCLUSTER=true]
#   make uninstall-osac  NS=osac  [VCLUSTER=true]
#   make uninstall-infra PLATFORM=kind|openshift  PROFILE=dev|vmaas-ci|bmaas-ci|caas-ci|full-ci
#   make test            SUITE=all|fulfillment|operator|bmf  NS=osac

# -- Fail-fast parameter validation ------------------------------------
# Guards fire at parse time, gated by MAKECMDGOALS so `make help` works.

PLATFORM_TARGETS := install uninstall install-infra uninstall-infra
PROFILE_TARGETS  := install uninstall install-infra uninstall-infra install-osac
NS_TARGETS       := install install-osac uninstall-osac uninstall test
SUITE_TARGETS    := test

ifneq ($(filter $(PLATFORM_TARGETS),$(MAKECMDGOALS)),)
ifndef PLATFORM
$(error PLATFORM is required (kind|openshift))
endif
ifeq ($(filter $(PLATFORM),kind openshift),)
$(error PLATFORM=$(PLATFORM) is invalid; must be kind or openshift)
endif
endif

ifneq ($(filter $(PROFILE_TARGETS),$(MAKECMDGOALS)),)
ifndef PROFILE
$(error PROFILE is required (dev|vmaas-ci|bmaas-ci|caas-ci|full-ci))
endif
ifeq ($(filter $(PROFILE),dev vmaas-ci bmaas-ci caas-ci full-ci),)
$(error PROFILE=$(PROFILE) is invalid; must be dev, vmaas-ci, bmaas-ci, caas-ci, or full-ci)
endif
endif

ifneq ($(filter $(PLATFORM_TARGETS),$(MAKECMDGOALS)),)
ifeq ($(PLATFORM),kind)
ifneq ($(PROFILE),dev)
$(error PLATFORM=kind only supports PROFILE=dev)
endif
endif
endif

ifneq ($(filter $(NS_TARGETS),$(MAKECMDGOALS)),)
ifndef NS
$(error NS is required (namespace for OSAC instance, e.g. NS=osac))
endif
endif

ifneq ($(filter $(SUITE_TARGETS),$(MAKECMDGOALS)),)
ifndef SUITE
$(error SUITE is required (all|fulfillment|operator|bmf))
endif
ifeq ($(filter $(SUITE),all fulfillment operator bmf),)
$(error SUITE=$(SUITE) is invalid; must be all, fulfillment, operator, or bmf)
endif
endif

# -- Paths and constants -----------------------------------------------

OSAC_INSTALLER := osac-installer
CHARTS         := $(OSAC_INSTALLER)/charts
DEPS_CHART     := $(CHARTS)/osac-deps
INFRA_CHART    := $(CHARTS)/osac-infra
OSAC_CHART     := $(CHARTS)/osac

EXTRA_HELM_ARGS ?=
VCLUSTER ?=

# vcluster lifecycle script.
VCLUSTER_SH := $(OSAC_INSTALLER)/scripts/vcluster.sh

# Every deployment is an instance. Derive instancePrefix from NS
# (hyphens to underscores for PostgreSQL identifier compatibility).
INSTANCE_PREFIX := $(subst -,_,$(NS))
INSTANCE_ARGS := \
  --set instancePrefix=$(INSTANCE_PREFIX) \
  --set operator.aap.templatePrefix=$(INSTANCE_PREFIX)-osac \
  --set metering.topicPrefix=$(INSTANCE_PREFIX)

# Values file selection: kind uses kind-specific files, vcluster adds an overlay.
ifeq ($(PLATFORM),kind)
INFRA_VALUES    := $(OSAC_INSTALLER)/values/dev/kind-infra.yaml
INSTANCE_VALUES := $(OSAC_INSTALLER)/values/dev/kind-instance.yaml
else ifdef PROFILE
INFRA_VALUES    := $(OSAC_INSTALLER)/values/$(PROFILE)/infra.yaml
INSTANCE_VALUES := $(OSAC_INSTALLER)/values/$(PROFILE)/instance.yaml
endif

# vcluster overlay: layer vcluster-instance.yaml on top of the profile's instance values.
VCLUSTER_INSTANCE_VALUES := $(OSAC_INSTALLER)/values/$(PROFILE)/vcluster-instance.yaml
VCLUSTER_HELM_ARGS := $(if $(filter true,$(VCLUSTER)),\
  $(if $(wildcard $(VCLUSTER_INSTANCE_VALUES)),-f $(VCLUSTER_INSTANCE_VALUES)),)

# Container tool and Kind cluster config.
CONTAINER_TOOL ?= $(or $(shell command -v podman 2>/dev/null),$(shell command -v docker 2>/dev/null),$(error neither podman nor docker found in PATH))
KIND_CLUSTER_NAME ?= osac-dev
KIND_CONFIG := kind-dev/kind-config.yaml
KIND_KUBECONFIG := $(HOME)/.kube/$(KIND_CLUSTER_NAME)-kind.kubeconfig
export KUBECONFIG ?= $(KIND_KUBECONFIG)

define kind-load-image
	@if [ "$(CONTAINER_TOOL)" = "podman" ]; then \
		tmpfile=$$(mktemp /tmp/kind-image-XXXXXX.tar); \
		$(CONTAINER_TOOL) save $(1) -o "$$tmpfile"; \
		kind load image-archive "$$tmpfile" --name $(KIND_CLUSTER_NAME); \
		rm -f "$$tmpfile"; \
	else \
		kind load docker-image $(1) --name $(KIND_CLUSTER_NAME); \
	fi
endef

# -- Help --------------------------------------------------------------

.PHONY: help
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Entity: Infra

.PHONY: install-infra
install-infra: ## Install infrastructure (PLATFORM= PROFILE= required)
ifeq ($(PLATFORM),kind)
	@if kind get clusters | grep -q "^$(KIND_CLUSTER_NAME)$$"; then \
		echo "Kind cluster '$(KIND_CLUSTER_NAME)' already exists"; \
	else \
		echo "Creating Kind cluster '$(KIND_CLUSTER_NAME)'..."; \
		kind create cluster --name $(KIND_CLUSTER_NAME) --config $(KIND_CONFIG) --wait 60s; \
	fi
	@kind export kubeconfig --name $(KIND_CLUSTER_NAME) --kubeconfig $(KIND_KUBECONFIG)
	helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
		--version v1.20.0 --namespace cert-manager --create-namespace \
		--set crds.enabled=true --wait --timeout 5m
	helm upgrade --install trust-manager oci://quay.io/jetstack/charts/trust-manager \
		--version v0.22.0 --namespace cert-manager \
		--set defaultPackage.enabled=false --wait --timeout 5m
	helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
		--version v1.6.5 --namespace envoy-gateway --create-namespace \
		--wait --timeout 5m
	helm upgrade --install osac-infra $(INFRA_CHART) \
		--namespace osac-infra --create-namespace \
		-f $(INFRA_VALUES) \
		--set osacNamespace=$(NS) \
		--wait-for-jobs --timeout 30m
	@for f in osac-operator/config/crd/fakes/*.yaml; do \
		case "$$(basename "$$f")" in \
			*osac.openshift.io*) continue ;; \
		esac; \
		kubectl apply --server-side --force-conflicts -f "$$f"; \
	done
else
	$(eval OCP_VERSION := $(shell oc get clusterversion version -o jsonpath='{.status.desired.version}' | cut -d. -f1,2))
	@bash -c 'source $(OSAC_INSTALLER)/scripts/lib.sh && \
		for ns in ansible-aap cert-manager-operator cert-manager openshift-storage metallb-system multicluster-engine openshift-cnv; do \
			wait_for_namespace_cleanup "$$ns"; \
		done'
	helm upgrade --install osac-deps $(DEPS_CHART) \
		--namespace osac-deps --create-namespace \
		--values $(INFRA_VALUES) \
		--set lvms.channel=stable-$(OCP_VERSION) \
		--timeout 30m --wait
	@bash -c 'source $(OSAC_INSTALLER)/scripts/lib.sh && wait_for_namespace_cleanup keycloak'
	$(eval DOMAIN := $(shell oc get ingresses.config/cluster -o jsonpath='{.spec.domain}'))
	@[ -n "$(DOMAIN)" ] || { echo "ERROR: could not determine cluster domain" >&2; exit 1; }
	helm upgrade --install osac-infra $(INFRA_CHART) \
		--namespace osac-infra --create-namespace \
		--values $(INFRA_VALUES) \
		--set osacNamespace=$(NS) \
		--set lvms.channel=stable-$(OCP_VERSION) \
		--set keycloak.hostname=https://keycloak-keycloak.$(DOMAIN) \
		--set keycloak.route.hostname=keycloak-keycloak.$(DOMAIN) \
		--timeout 30m --wait-for-jobs
endif

.PHONY: uninstall-infra
uninstall-infra: ## Uninstall infrastructure (PLATFORM= PROFILE= required)
ifeq ($(PLATFORM),kind)
	helm uninstall osac-infra --namespace osac-infra --ignore-not-found
	helm uninstall envoy-gateway --namespace envoy-gateway --ignore-not-found
	helm uninstall trust-manager --namespace cert-manager --ignore-not-found
	helm uninstall cert-manager --namespace cert-manager --ignore-not-found
	kind delete cluster --name $(KIND_CLUSTER_NAME)
else
	helm uninstall osac-infra --namespace osac-infra --ignore-not-found --wait --timeout 10m
	helm uninstall osac-deps --namespace osac-deps --ignore-not-found
endif

##@ Entity: Osac

.PHONY: install-osac
install-osac: ## Install OSAC instance (PROFILE= NS= required, VCLUSTER=true for vcluster)
	cd $(OSAC_INSTALLER) && helm dependency build $(subst $(OSAC_INSTALLER)/,,$(OSAC_CHART))
ifeq ($(VCLUSTER),true)
	VCLUSTER_NAME=$(NS) VCLUSTER_NS=$(NS) $(VCLUSTER_SH) create
	VCLUSTER_NAME=$(NS) VCLUSTER_NS=$(NS) OSAC_NS=$(NS) $(VCLUSTER_SH) setup
	$(eval VCLUSTER_KC := /tmp/vcluster-$(NS).kubeconfig)
endif
ifneq ($(PLATFORM),kind)
	$(eval DOMAIN := $(shell oc get ingresses.config/cluster -o jsonpath='{.spec.domain}'))
	@[ -n "$(DOMAIN)" ] || { echo "ERROR: Could not determine cluster domain. Is oc logged in?" >&2; exit 1; }
endif
	helm upgrade --install osac $(OSAC_CHART) \
		-f $(INSTANCE_VALUES) \
		$(VCLUSTER_HELM_ARGS) \
		--namespace $(NS) --create-namespace \
		$(if $(VCLUSTER_KC),--kubeconfig $(VCLUSTER_KC)) \
		$(if $(DOMAIN),--set service.externalHostname=fulfillment-api-$(NS).$(DOMAIN) --set service.internalHostname=fulfillment-internal-api-$(NS).$(DOMAIN) --set service.auth.issuerUrl=https://keycloak-keycloak.$(DOMAIN)/realms/osac) \
		$(INSTANCE_ARGS) \
		$(EXTRA_HELM_ARGS) \
		--wait --timeout 40m

.PHONY: uninstall-osac
uninstall-osac: ## Uninstall OSAC instance (NS= required, VCLUSTER=true for vcluster)
ifeq ($(VCLUSTER),true)
	VCLUSTER_NAME=$(NS) VCLUSTER_NS=$(NS) $(VCLUSTER_SH) teardown
else
	helm uninstall osac --namespace $(NS) --ignore-not-found
endif

##@ Composite

.PHONY: install
install: install-infra install-osac ## Full install: infra + osac (PLATFORM= PROFILE= required)

.PHONY: uninstall
uninstall: uninstall-osac uninstall-infra ## Full uninstall: osac + infra (PLATFORM= PROFILE= required)

##@ Entity: Test

.PHONY: test
test: ## Run integration tests (SUITE= required: all|fulfillment|operator|bmf)
ifeq ($(SUITE),all)
	$(MAKE) test SUITE=fulfillment
	$(MAKE) test SUITE=operator
	$(MAKE) test SUITE=bmf
endif
ifeq ($(SUITE),fulfillment)
	$(CONTAINER_TOOL) build -t localhost/fulfillment-service:it -f fulfillment-service/Containerfile .
	$(call kind-load-image,localhost/fulfillment-service:it)
	helm upgrade --install osac $(OSAC_CHART) \
		-f $(OSAC_INSTALLER)/values/dev/kind-instance.yaml \
		--set service.images.service=localhost/fulfillment-service:it \
		--namespace $(NS) --wait --timeout 5m
	@for f in fulfillment-service/it/crds/*.yaml; do kubectl apply --server-side --force-conflicts -f "$$f"; done
	cd fulfillment-service && ginkgo run --timeout 1h -v it
endif
ifeq ($(SUITE),operator)
	$(CONTAINER_TOOL) build -t localhost/osac-operator:it -f osac-operator/Containerfile .
	$(call kind-load-image,localhost/osac-operator:it)
	helm upgrade --install osac $(OSAC_CHART) \
		-f $(OSAC_INSTALLER)/values/dev/kind-instance.yaml \
		--set operator.image.repository=localhost/osac-operator \
		--set operator.image.tag=it \
		--set operator.image.pullPolicy=Never \
		--namespace $(NS) --wait --timeout 5m
	cd osac-operator && ginkgo run --timeout 30m -v test/integration
endif
ifeq ($(SUITE),bmf)
	$(CONTAINER_TOOL) build -t localhost/bmf-operator:it -f bare-metal-fulfillment-operator/Containerfile .
	$(call kind-load-image,localhost/bmf-operator:it)
	kubectl apply --server-side --force-conflicts -f bare-metal-fulfillment-operator/test/crds/
	helm upgrade --install osac $(OSAC_CHART) \
		-f $(OSAC_INSTALLER)/values/dev/kind-instance.yaml \
		--set bmf.enabled=true \
		--set bmf.image.repository=localhost/bmf-operator \
		--set bmf.image.tag=it \
		--set bmf.image.pullPolicy=Never \
		--namespace $(NS) --wait --timeout 5m
	cd bare-metal-fulfillment-operator && ginkgo run --timeout 30m -v test/integration
endif
