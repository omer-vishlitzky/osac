# osac-installer helper targets.
# Deployment targets (install, uninstall) live in the root Makefile.

INSTALLER_NAMESPACE ?= osac
# Extra helm --set/--set-string args appended to install commands.
EXTRA_HELM_ARGS ?=

.PHONY: help
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

##@ Pre-install Helpers

.PHONY: wait-for-api
wait-for-api: ## Wait for the Kubernetes API server to be consistently reachable
	@echo "Waiting for API server to be reachable..."
	@for i in $$(seq 1 30); do \
		if oc get --raw /version >/dev/null 2>&1; then \
			echo "API server is reachable."; \
			exit 0; \
		fi; \
		if [ "$$i" -eq 30 ]; then \
			echo "ERROR: API server still unreachable after 5 minutes." >&2; \
			exit 1; \
		fi; \
		echo "  Not yet reachable (attempt $$i/30), retrying in 10s..."; \
		sleep 10; \
	done

.PHONY: wait-for-deps-namespaces
wait-for-deps-namespaces: ## Wait for operator namespaces left Terminating by a prior uninstall
	@bash -c 'source scripts/lib.sh && \
		for ns in ansible-aap cert-manager-operator cert-manager openshift-storage metallb-system multicluster-engine openshift-cnv; do \
			wait_for_namespace_cleanup "$$ns"; \
		done'

.PHONY: wait-for-infra-namespaces
wait-for-infra-namespaces: ## Wait for the keycloak namespace left Terminating by a prior uninstall
	@bash -c 'source scripts/lib.sh && wait_for_namespace_cleanup keycloak'

AAP_LICENSE_FILE ?= license.zip

.PHONY: install-secrets
install-secrets: ## Create pre-install secrets (AAP license)
	@[[ -f "$(AAP_LICENSE_FILE)" ]] || { echo "ERROR: AAP license not found at $(AAP_LICENSE_FILE)"; exit 1; }
	@bash -c 'source scripts/lib.sh && wait_for_namespace_cleanup "$(INSTALLER_NAMESPACE)"'
	oc create namespace $(INSTALLER_NAMESPACE) --dry-run=client -o yaml | oc apply -f -
	oc create secret generic config-as-code-manifest-ig \
		--from-file=license.zip="$(AAP_LICENSE_FILE)" \
		-n $(INSTALLER_NAMESPACE) --dry-run=client -o yaml | oc apply --server-side -f -
	oc label secret config-as-code-manifest-ig \
		osac.openshift.io/project=osac-aap \
		-n $(INSTALLER_NAMESPACE) --overwrite

.PHONY: check-postgres
check-postgres: ## Verify in-cluster PostgreSQL is reachable before installing OSAC
	@bash -c 'source scripts/lib.sh && check_postgres_prerequisites "$(INSTALLER_NAMESPACE)" "$(VALUES_FILE)"'

##@ Helm Chart Management

.PHONY: sync-charts
sync-charts: ## Update submodules and rebuild chart dependencies
	git submodule update --init --recursive
	helm dependency build charts/osac/

.PHONY: helm-deps
helm-deps: ## Build Helm chart dependencies
	helm dependency build charts/osac/

.PHONY: helm-lint
helm-lint: ## Lint all charts
	helm lint charts/osac-deps/
	helm lint charts/osac-infra/
	helm dependency build charts/osac/
	helm lint charts/osac/

.PHONY: helm-template
helm-template: ## Dry-run render all templates
	helm template osac-deps charts/osac-deps/
	helm template osac-infra charts/osac-infra/
	helm template osac charts/osac/

.PHONY: helm-validate
helm-validate: helm-lint ## Validate all charts (lint + template)
	@for d in values/*/; do \
		infra="$${d}infra.yaml"; \
		[ -f "$$infra" ] && echo "Validating $$infra..." && \
			helm template osac-deps charts/osac-deps/ --values "$$infra" > /dev/null && \
			helm template osac-infra charts/osac-infra/ --values "$$infra" > /dev/null; \
	done
	./scripts/validate-keycloak-credentials.sh
	@echo "Validation passed."
