"""Deployment credential jobs must use the production GitHub Environment."""

from pathlib import Path
import re

import yaml


DEPLOY_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
)
DEPLOYMENT_CREDENTIALS = {
    "SSH_DEPLOY_KEY",
    "KNOWN_HOSTS",
    "ACR_USERNAME",
    "ACR_PASSWORD",
}
CREDENTIAL_REFERENCE = re.compile(
    r"secrets\.({})\b".format("|".join(sorted(DEPLOYMENT_CREDENTIALS)))
)


def _string_values(value):
    if isinstance(value, dict):
        for key, nested_value in value.items():
            yield from _string_values(key)
            yield from _string_values(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            yield from _string_values(nested_value)
    elif isinstance(value, str):
        yield value


def test_jobs_reading_deployment_credentials_use_production_environment():
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    credential_jobs = {
        job_name: {
            credential
            for value in _string_values(job)
            for credential in CREDENTIAL_REFERENCE.findall(value)
        }
        for job_name, job in jobs.items()
    }
    credential_jobs = {
        job_name: credentials
        for job_name, credentials in credential_jobs.items()
        if credentials
    }

    assert credential_jobs, "workflow has no deployment credential references"
    unbound_jobs = {
        job_name: credentials
        for job_name, credentials in credential_jobs.items()
        if jobs[job_name].get("environment") != "production"
        and jobs[job_name].get("with", {}).get("environment") != "production"
    }

    assert not unbound_jobs, (
        "jobs reading deployment credentials must bind the production Environment: "
        f"{unbound_jobs}"
    )
