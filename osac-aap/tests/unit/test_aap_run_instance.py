"""The AAP chart's run-instance wiring: global.instanceId and aap.vcluster.*.

One AAP serves many shared-infrastructure E2E runs, so a run names its AAP
organization after its run ID (global.instanceId) and gets its job pods
created inside its own vcluster (aap.vcluster.apiServerUrl plus the token
Secret). Both settings default empty and must leave the render byte-identical
to the vcluster-e2e base, captured in fixtures/aap_vcluster/.

The same runs also create no recurring AAP schedules: configAsCode.
schedulesEnabled reaches config-as-code as OSAC_AAP_SCHEDULES_ENABLED and
empties controller_schedules, so nothing is left behind after a failed
teardown.
"""

import subprocess
import tempfile
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_PATH = Path(__file__).resolve().parents[2] / "charts/aap"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "aap_vcluster"
CONFIG_AS_CODE = (
    REPO_ROOT
    / "osac-aap/collections/ansible_collections/osac/config_as_code"
)
CONFIG_VARS = CONFIG_AS_CODE / "playbooks" / "vars" / "config.yml"
CONTROLLER_VARS = CONFIG_AS_CODE / "roles" / "aap" / "vars" / "controller.yml"
VCLUSTER_CI_PROFILE = REPO_ROOT / "osac-installer/values/vcluster-ci/instance.yaml"

INSTANCE = "pr2100v1"
VCLUSTER_ARGS = (
    f"vcluster.apiServerUrl=https://{INSTANCE}.vc-{INSTANCE}.svc.cluster.local",
    f"vcluster.tokenSecret.name={INSTANCE}-vcluster-token",
    "vcluster.tokenSecret.key=token",
)
EXTERNAL_ARGS = (
    "aap.instance.enabled=false",
    "controllerUrl=https://aap.example.com",
    "bootstrap.adminPasswordSecret.name=pr2100v1-aap-admin",
    "bootstrap.adminPasswordSecret.key=admin-password",
)


def _render(*set_strings, show_only=None, values_file=None):
    args = ["helm", "template", "aap-test", str(CHART_PATH)]
    if show_only:
        args += ["--show-only", show_only]
    if values_file:
        args += ["-f", str(values_file)]
    for value in set_strings:
        args += ["--set", value]
    return subprocess.run(args, capture_output=True, text=True)


def _bootstrap_job(*set_strings):
    result = _render(*set_strings, show_only="templates/bootstrap-job.yaml")
    assert result.returncode == 0, result.stderr
    return list(yaml.safe_load_all(result.stdout))


def _secret(*set_strings, values_file=None):
    result = _render(
        *set_strings,
        show_only="templates/config-as-code-secret.yaml",
        values_file=values_file,
    )
    assert result.returncode == 0, result.stderr
    return next(doc for doc in yaml.safe_load_all(result.stdout) if doc)


def _env_map(container):
    return {item["name"]: item for item in container["env"]}


def _normalize(text):
    """helm's wait-for-aap script renders lines with trailing spaces; the
    repo's trailing-whitespace hook strips them from the committed goldens,
    so compare with them stripped on both sides."""
    return "\n".join(line.rstrip() for line in text.splitlines())


def test_unset_settings_render_byte_identical_to_the_vcluster_e2e_base():
    """Every vcluster wiring omits cleanly: regenerate the goldens with

    helm template aap-test charts/aap --show-only templates/bootstrap-job.yaml \
        > tests/unit/fixtures/aap_vcluster/bootstrap-job-default.yaml
    and the external-AAP and secret variants below, after a chart change.
    """
    cases = [
        ([], "bootstrap-job-default.yaml"),
        (list(EXTERNAL_ARGS), "bootstrap-job-external.yaml"),
    ]
    for set_strings, golden in cases:
        result = _render(*set_strings, show_only="templates/bootstrap-job.yaml")
        assert result.returncode == 0, result.stderr
        assert _normalize(result.stdout) == _normalize(
            (FIXTURES / golden).read_text()
        ), golden

    result = _render(show_only="templates/config-as-code-secret.yaml")
    assert result.returncode == 0, result.stderr
    assert _normalize(result.stdout) == _normalize(
        (FIXTURES / "config-as-code-secret-default.yaml").read_text()
    )


def test_bootstrap_carries_the_fulfillment_secret_reader_credential():
    """config-as-code asserts this variable whenever the reader ConfigMap is
    rendered, so the bootstrap Job must always carry it.

    The goldens cannot catch its removal on their own: a golden regenerated
    from a broken render matches the broken render. This asserts the entry
    itself, independently of any committed output.
    """
    for set_strings in ([], list(EXTERNAL_ARGS)):
        job = next(doc for doc in _bootstrap_job(*set_strings) if doc["kind"] == "Job")
        containers = job["spec"]["template"]["spec"]["containers"]
        bootstrap = next(c for c in containers if c["name"] == "bootstrap")
        entry = _env_map(bootstrap).get("OSAC_AAP_SECRET_READER_CLIENT_SECRET")
        assert entry is not None, f"reader credential missing with {set_strings}"
        assert entry["valueFrom"]["secretKeyRef"] == {
            "name": "aap-fulfillment-secret-reader",
            "key": "client-secret",
            "optional": True,
        }


def test_instance_id_names_the_config_as_code_run():
    secret = _secret(f"global.instanceId={INSTANCE}")

    assert secret["stringData"]["OSAC_INSTANCE_ID"] == INSTANCE
    # The organization and prefix follow in config-as-code (config.yml
    # defaults them to the run ID), not through more chart env.
    assert not any(key.startswith("AAP_ORGANIZATION_NAME") for key in secret["stringData"])
    assert not any(key.startswith("AAP_PREFIX") for key in secret["stringData"])


def test_unset_instance_id_adds_no_secret_key():
    secret = _secret()

    assert "OSAC_INSTANCE_ID" not in secret["stringData"]


def test_vcluster_settings_reach_the_bootstrap_job():
    job = next(doc for doc in _bootstrap_job(*VCLUSTER_ARGS) if doc["kind"] == "Job")
    spec = job["spec"]["template"]["spec"]

    bootstrap = next(c for c in spec["containers"] if c["name"] == "bootstrap")
    env = _env_map(bootstrap)
    assert env["VCLUSTER_API_URL"]["value"] == (
        f"https://{INSTANCE}.vc-{INSTANCE}.svc.cluster.local"
    )
    assert env["VCLUSTER_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{INSTANCE}-vcluster-token",
        "key": "token",
    }

    wait_for_token = next(
        c for c in spec["initContainers"] if c["name"] == "wait-for-vcluster-token"
    )
    assert _env_map(wait_for_token)["VCLUSTER_TOKEN_SECRET_NAME"]["value"] == (
        f"{INSTANCE}-vcluster-token"
    )
    script = wait_for_token["command"][2]
    assert "oc get secret" in script
    assert "exit 1" in script


def test_vcluster_settings_add_the_wait_containers_rbac_rule():
    docs = _bootstrap_job(*VCLUSTER_ARGS)
    role = next(doc for doc in docs if doc["kind"] == "Role")
    token_rule = next(
        rule for rule in role["rules"] if rule["resourceNames"] == [f"{INSTANCE}-vcluster-token"]
    )
    assert token_rule["verbs"] == ["get"]


def test_unset_vcluster_settings_keep_the_rbac_and_containers_as_today():
    docs = _bootstrap_job()
    role = next(doc for doc in docs if doc["kind"] == "Role")
    assert len(role["rules"]) == 1

    job = next(doc for doc in docs if doc["kind"] == "Job")
    spec = job["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["wait-for-admin-secret", "wait-for-aap"]
    bootstrap = next(c for c in spec["containers"] if c["name"] == "bootstrap")
    assert "VCLUSTER_API_URL" not in _env_map(bootstrap)
    assert "VCLUSTER_TOKEN" not in _env_map(bootstrap)


def test_vcluster_settings_reach_the_config_as_code_secret():
    secret = _secret(*VCLUSTER_ARGS)

    assert secret["stringData"]["VCLUSTER_API_URL"] == (
        f"https://{INSTANCE}.vc-{INSTANCE}.svc.cluster.local"
    )
    assert secret["stringData"]["VCLUSTER_TOKEN_SECRET_NAME"] == f"{INSTANCE}-vcluster-token"
    assert secret["stringData"]["VCLUSTER_TOKEN_SECRET_KEY"] == "token"
    # The token itself never passes through chart values.
    assert "VCLUSTER_TOKEN" not in secret["stringData"]


def test_unset_vcluster_settings_add_no_secret_keys():
    secret = _secret()

    assert not any(key.startswith("VCLUSTER_") for key in secret["stringData"])


def test_half_set_vcluster_pair_fails_the_render_naming_what_is_missing():
    url_only = _render("vcluster.apiServerUrl=https://pr2100v1.vc-pr2100v1.svc.cluster.local")
    assert url_only.returncode != 0
    assert "vcluster.tokenSecret.name is required when vcluster.apiServerUrl is set" in (
        url_only.stderr
    )

    name_only = _render("vcluster.tokenSecret.name=pr2100v1-vcluster-token")
    assert name_only.returncode != 0
    assert "vcluster.apiServerUrl is required when vcluster.tokenSecret is set" in (
        name_only.stderr
    )

    no_key = _render(
        "vcluster.apiServerUrl=https://pr2100v1.vc-pr2100v1.svc.cluster.local",
        "vcluster.tokenSecret.name=pr2100v1-vcluster-token",
    )
    assert no_key.returncode != 0
    assert "vcluster.tokenSecret.key is required when vcluster.apiServerUrl is set" in (
        no_key.stderr
    )


def test_malformed_vcluster_url_fails_the_render():
    result = _render(
        "vcluster.apiServerUrl=https://pr2100v1.vc-pr2100v1.svc.cluster.local/api/controller",
        "vcluster.tokenSecret.name=pr2100v1-vcluster-token",
        "vcluster.tokenSecret.key=token",
    )

    assert result.returncode != 0
    assert "vcluster.apiServerUrl must be an https:// URL without a path" in result.stderr


def test_plain_http_vcluster_url_fails_the_render():
    # The controller sends the token to this URL as a bearer credential.
    result = _render(
        "vcluster.apiServerUrl=http://pr2100v1.vc-pr2100v1.svc.cluster.local",
        "vcluster.tokenSecret.name=pr2100v1-vcluster-token",
        "vcluster.tokenSecret.key=token",
    )

    assert result.returncode != 0
    assert "vcluster.apiServerUrl must be an https:// URL without a path" in result.stderr


# --- The recurring AAP schedules (configAsCode.schedulesEnabled) ---

SCHEDULE_NAMES = ["osac-sync", "osac-config-as-code", "osac-publish-templates"]

# A developer shell exporting any of these must not change the render.
AMBIENT_ENV = (
    "NAMESPACE",
    "AAP_INSTANCE_NAME",
    "AAP_HOSTNAME",
    "AAP_USERNAME",
    "AAP_PASSWORD",
    "AAP_ORGANIZATION_NAME",
    "AAP_PREFIX",
    "AAP_PROJECT_NAME",
    "OSAC_INSTANCE_ID",
    "OSAC_AAP_SCHEDULES_ENABLED",
    "OSAC_PUBLISH_TEMPLATES_ENABLED",
)


def _schedules(monkeypatch, *set_strings, values_file=None):
    """config-as-code's controller_schedules, driven by the chart Secret.

    The bootstrap job and the config-as-code rerun read the Secret through
    envFrom, so its stringData is exactly the environment the schedules see.
    """
    secret = _secret(*set_strings, values_file=values_file)
    for name in AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in secret["stringData"].items():
        monkeypatch.setenv(name, str(value))

    variables = {
        **yaml.safe_load(CONFIG_VARS.read_text()),
        **yaml.safe_load(CONTROLLER_VARS.read_text()),
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(variables["controller_schedules"])


def test_the_default_secret_creates_the_three_recurring_schedules(monkeypatch):
    schedules = _schedules(monkeypatch)

    assert [schedule["name"] for schedule in schedules] == SCHEDULE_NAMES
    assert [schedule["rrule"] for schedule in schedules] == [
        "DTSTART:20250331T144500Z RRULE:FREQ=MINUTELY;INTERVAL=10",
        "DTSTART:20250331T144500Z RRULE:FREQ=HOURLY;INTERVAL=1",
        "DTSTART:20250513T111500Z RRULE:FREQ=MINUTELY;INTERVAL=30",
    ]


def test_the_default_secret_omits_the_schedules_env():
    """The value defaults to true, so leaving it out keeps every profile's
    Secret byte-identical to the committed golden."""
    assert "OSAC_AAP_SCHEDULES_ENABLED" not in _secret()["stringData"]


def test_disabling_schedules_empties_the_list(monkeypatch):
    secret = _secret("configAsCode.schedulesEnabled=false")

    assert secret["stringData"]["OSAC_AAP_SCHEDULES_ENABLED"] == "false"
    assert _schedules(monkeypatch, "configAsCode.schedulesEnabled=false") == []


def test_the_vcluster_ci_profile_creates_no_schedules(monkeypatch):
    """Short-lived CI runs share one AAP, so the recurring jobs would add
    load for the run's whole life and outlive a failed teardown."""
    profile = yaml.safe_load(VCLUSTER_CI_PROFILE.read_text())
    assert profile["aap"]["configAsCode"]["schedulesEnabled"] is False

    with tempfile.TemporaryDirectory() as tmp:
        values_file = Path(tmp) / "aap.yaml"
        values_file.write_text(yaml.safe_dump(profile["aap"]))
        schedules = _schedules(
            monkeypatch, *EXTERNAL_ARGS, values_file=values_file
        )
    assert schedules == []


# --- The roles' run-ID plumbing (group_vars/all/osac_instance.yaml) ---

AAP_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_VARS = AAP_ROOT / "group_vars" / "all" / "osac_instance.yaml"
CSI_DEFAULTS = (
    AAP_ROOT
    / "collections/ansible_collections/osac/service/roles/csi_driver_install/defaults/main.yaml"
)
LVMS_DEFAULTS = (
    AAP_ROOT
    / "collections/ansible_collections/osac/templates/roles/lvms_storage/defaults/main.yaml"
)
INSTANCE_LABEL = "osac.openshift.io/instance"


def _render_vars(monkeypatch, instance_id):
    monkeypatch.delenv("OSAC_INSTANCE_ID", raising=False)
    if instance_id is not None:
        monkeypatch.setenv("OSAC_INSTANCE_ID", instance_id)
    variables = yaml.safe_load(INSTANCE_VARS.read_text())
    templar = Templar(loader=DataLoader(), variables=variables)
    return {key: templar.template(value) for key, value in variables.items()}


def _templar_with_instance(instance_id):
    variables = {**yaml.safe_load(INSTANCE_VARS.read_text()), "osac_instance_id": instance_id}
    return Templar(loader=DataLoader(), variables=variables)


def test_the_run_id_label_var_follows_the_env(monkeypatch):
    rendered = _render_vars(monkeypatch, "pr2100v1")

    assert rendered["osac_instance_id"] == "pr2100v1"
    assert rendered["osac_instance_labels"] == {INSTANCE_LABEL: "pr2100v1"}


def test_without_a_run_id_the_label_var_is_empty(monkeypatch):
    rendered = _render_vars(monkeypatch, None)

    assert rendered["osac_instance_id"] == ""
    assert rendered["osac_instance_labels"] == {}


def test_the_storage_class_prefix_carries_the_run_id():
    templar = _templar_with_instance("pr2100v1")

    csi = yaml.safe_load(CSI_DEFAULTS.read_text())
    assert templar.template(csi["csi_driver_install_storage_class_prefix"]) == "osac-pr2100v1-"

    lvms = yaml.safe_load(LVMS_DEFAULTS.read_text())
    assert templar.template(lvms["lvms_storage_storage_class_prefix"]) == "osac-pr2100v1-"
    # The tenant teardown selects by the instance label, so it never deletes
    # another run's classes with the same tenant name.
    assert templar.template(lvms["lvms_storage_instance_selectors"]) == [
        "osac.openshift.io/instance=pr2100v1"
    ]


def test_without_a_run_id_the_storage_class_prefix_stays_osac():
    templar = _templar_with_instance("")

    csi = yaml.safe_load(CSI_DEFAULTS.read_text())
    assert templar.template(csi["csi_driver_install_storage_class_prefix"]) == "osac-"

    lvms = yaml.safe_load(LVMS_DEFAULTS.read_text())
    assert templar.template(lvms["lvms_storage_storage_class_prefix"]) == "osac-"
    assert templar.template(lvms["lvms_storage_instance_selectors"]) == []
