#!/usr/bin/env python3
"""Allocate a VLAN segment for every hosted cluster this branch adds or changes,
through the orchestrator's allocate-segment workflow.

Replaces cluster_network_auto_configurator.py (the old in-house VLAN
allocator). It runs as a CI step on the pipeline's own branch, which is never
main:

  1. find the sites/*/mces/*/hostedClusters/<cluster>.yaml files this branch
     adds or modifies relative to main;
  2. for each one that still needs a VLAN, start the workflow:
     POST /workflows/segment-lifecycle/allocate-segment
          {"cluster": "<file stem>", "values_branch": "<this branch>"};
  3. poll GET /workflows/runs/{workflow_id} until the run closes;
  4. with --pull, fast-forward the checkout to the workflow's commit, so later
     steps (generate_vlan_butane.sh) read the vlanId it recorded.

The workflow itself records the allocation: it appends the vlanId +
dhcp_values block to the cluster file on THIS branch and pushes with a
CI-skip marker, so its commit starts no pipeline. The DHCP scope appears only
after the branch is merged to main (Argo CD reads main only).

A file does NOT get an allocation when:
  * it sets `AutomateAllocation: false` (the operator assigns the vlanId);
  * it already carries the workflow's marker block (allocated earlier);
  * it already has a top-level vlanId or dhcp_values written by hand.

Stdlib only, so it runs unchanged on the python-git-yq image and on a GitHub
runner. Configuration comes from the environment:
  WORKFLOWS_API_URL        base URL of workflows-orchestrator-api (required)
  CI_COMMIT_BRANCH / GITHUB_REF_NAME
                           default for --branch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ALLOCATE_PATH = "/workflows/segment-lifecycle/allocate-segment"
RUNS_PATH = "/workflows/runs/"
CLUSTER_FILE_RE = re.compile(r"^sites/[^/]+/mces/[^/]+/hostedClusters/[^/]+\.yaml$")

# What the workflow writes, and what a hand-assigned network looks like. Both
# are matched at column 0, exactly as the workflow itself checks the file.
WORKFLOW_MARKER = "# === Added By Segment-Allocation Workflow ==="
MANUAL_ALLOCATION_RE = re.compile(r"^(vlanId|dhcp_values)\s*:", re.MULTILINE)
AUTOMATE_OFF_RE = re.compile(r"^AutomateAllocation\s*:\s*false\s*(#.*)?$", re.MULTILINE | re.IGNORECASE)
CLUSTER_NAME_RE = re.compile(r"^clusterName\s*:\s*[\"']?([^\"'#\s]+)", re.MULTILINE)

TERMINAL_FAILURES = {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
HTTP_TIMEOUT_SECONDS = 30
# No response at all (0), or a router/gateway between us and the API failing.
RETRYABLE_START_STATUSES = {0, 502, 503, 504}
START_ATTEMPTS = 6  # 2+4+8+16+30 s of backoff, about a minute

# TLS verification is OFF, everywhere this runs. The air-gapped environment
# serves the orchestrator from an internal CA the CI images do not trust, so a
# verifying client fails every handshake there. One behaviour in every
# environment, the same decision the orchestrator's own clients make
# (workflows/activities/segment_lifecycle/activities.py, _TLS_VERIFY).
TLS_VERIFY = False


def log(message: str) -> None:
    print(message, flush=True)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


# --- which files ------------------------------------------------------------


def changed_cluster_files(main_branch: str) -> list[str]:
    """Cluster files this branch adds or modifies, compared with main.

    Three-dot diff: only what the BRANCH changed since it left main, never
    what main changed since. A shallow CI clone has no merge base, so it is
    deepened first.
    """
    git("fetch", "--no-tags", "origin", f"+refs/heads/{main_branch}:refs/remotes/origin/{main_branch}")
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        git("fetch", "--no-tags", "--unshallow", "origin")
    diff = git(
        "diff", "--name-only", "--diff-filter=AM", f"origin/{main_branch}...HEAD",
    ).stdout.splitlines()
    return sorted(path for path in diff if CLUSTER_FILE_RE.match(path))


def reason_to_skip(content: str) -> str | None:
    """Why this file gets no allocation, or None when it needs one."""
    if AUTOMATE_OFF_RE.search(content):
        return "AutomateAllocation is false (the vlanId is assigned by hand)"
    if WORKFLOW_MARKER in content:
        return "already allocated by the workflow"
    if MANUAL_ALLOCATION_RE.search(content):
        return "already has a hand-written vlanId or dhcp_values"
    return None


# --- the API ------------------------------------------------------------------


class Api:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.context = ssl.create_default_context() if TLS_VERIFY else ssl._create_unverified_context()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=self.context) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw or b"{}")
            except ValueError:
                return err.code, {"detail": raw.decode(errors="replace")}


def start_run(api: Api, cluster: str, branch: str, segment_type: str) -> str:
    """Start the workflow and return its id. A run already in flight for this
    cluster (409) is adopted: the id is deterministic, so it is polled the same.

    A dropped connection or a gateway error is retried. That is safe because
    of the same deterministic id: if a lost request did start the run, the
    retry answers 409 and adopts it, so no second run is ever created."""
    attempt = 0
    while True:
        attempt += 1
        try:
            status, body = api.request(
                "POST", ALLOCATE_PATH,
                {"cluster": cluster, "values_branch": branch, "type": segment_type},
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            status, body = 0, {"detail": str(exc)}
        if status not in RETRYABLE_START_STATUSES or attempt >= START_ATTEMPTS:
            break
        log(f"  start attempt {attempt} failed ({status or 'no response'}): {body.get('detail', body)} — retrying")
        time.sleep(min(2 ** attempt, 30))
    if status == 202:
        log(f"  started {body['workflow_id']}")
        return body["workflow_id"]
    if status == 409:
        workflow_id = f"allocate-segment-{segment_type}-{cluster}"
        log(f"  already running: {workflow_id} — waiting on that run")
        return workflow_id
    raise RuntimeError(f"start rejected ({status}): {body.get('detail', body)}")


def wait_for_run(api: Api, workflow_id: str, timeout: float, interval: float) -> dict:
    """Poll the run until it closes. Transient API errors are retried until the
    deadline; a run that ends in anything but COMPLETED raises."""
    deadline = time.monotonic() + timeout
    last_phase = None
    while True:
        try:
            status, body = api.request("GET", RUNS_PATH + workflow_id)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            status, body = 0, {"detail": str(exc)}
        if status == 200:
            phase = (body.get("progress") or {}).get("phase")
            if phase and phase != last_phase:
                log(f"  phase: {phase}")
                last_phase = phase
            run_status = body.get("status")
            if run_status == "COMPLETED":
                return body.get("result") or {}
            if run_status in TERMINAL_FAILURES:
                raise RuntimeError(
                    f"{workflow_id} {run_status} in phase {last_phase or '?'}: {body.get('error')}"
                )
        elif status != 404:  # 404 right after the start is the server catching up
            log(f"  status check failed ({status}): {body.get('detail', body)} — retrying")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{workflow_id} did not finish within {int(timeout)}s")
        time.sleep(interval)


# --- main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--branch",
        default=os.environ.get("CI_COMMIT_BRANCH") or os.environ.get("GITHUB_REF_NAME"),
        help="the values-repo branch to record on (default: the CI branch)",
    )
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--type", default="HC", help="segment type to allocate as (default HC)")
    parser.add_argument("--timeout", type=float, default=1200, help="seconds to wait per run")
    parser.add_argument("--interval", type=float, default=5, help="seconds between status checks")
    parser.add_argument("--pull", action="store_true", help="fast-forward to the workflow's commit afterwards")
    parser.add_argument("files", nargs="*", help="cluster files to consider (default: changed vs main)")
    args = parser.parse_args()

    api_url = os.environ.get("WORKFLOWS_API_URL", "").strip()
    if not api_url:
        log("ERROR: WORKFLOWS_API_URL is not set")
        return 1
    if not args.branch:
        log("ERROR: no branch — pass --branch or run inside CI")
        return 1
    if args.branch == args.main_branch:
        # The pipeline never runs on main, and main is only ever changed by a
        # reviewed merge: recording an allocation there directly is exactly
        # what this step must not do.
        log(f"ERROR: refusing to allocate onto {args.main_branch}")
        return 1
    api = Api(api_url)

    try:
        files = args.files or changed_cluster_files(args.main_branch)
    except RuntimeError as exc:
        log(f"ERROR: could not list this branch's cluster files: {exc}")
        return 1
    if not files:
        log("No hosted-cluster file added or changed on this branch — nothing to allocate.")
        return 0

    failures, allocated, skipped = [], [], 0
    for path in files:
        cluster = Path(path).stem
        log(f"{path}")
        content = Path(path).read_text()
        skip = reason_to_skip(content)
        if skip:
            log(f"  skipped: {skip}")
            skipped += 1
            continue
        declared = CLUSTER_NAME_RE.search(content)
        if declared and declared.group(1) != cluster:
            # The workflow allocates for the FILE NAME; the MC templates use
            # clusterName. A mismatch would give one cluster another's vlan.
            failures.append(f"{path}: clusterName {declared.group(1)!r} differs from the file name {cluster!r}")
            log(f"  ERROR: {failures[-1]}")
            continue
        try:
            workflow_id = start_run(api, cluster, args.branch, args.type)
            result = wait_for_run(api, workflow_id, args.timeout, args.interval)
        except RuntimeError as exc:
            failures.append(f"{path}: {exc}")
            log(f"  ERROR: {exc}")
            continue
        recorded_on = result.get("values_branch")
        if recorded_on != args.branch:
            # A 409 adopted a run another pipeline started: the workflow id is
            # per cluster, not per branch, so two branches defining the SAME
            # cluster share one run — and its block went to the other branch.
            failures.append(
                f"{path}: cluster {cluster} was allocated on branch {recorded_on!r} by another "
                f"pipeline, not on {args.branch!r} — two branches define the same cluster"
            )
            log(f"  ERROR: {failures[-1]}")
            continue
        log(
            f"  allocated {result.get('segment')} vlan {result.get('vlan_id')} at "
            f"{result.get('site')} — {'pushed ' + str(result.get('commit_sha'))[:10] if result.get('values_updated') else 'already recorded'}"
            f" on {recorded_on}"
        )
        allocated.append(path)

    if args.pull and allocated:
        pull = git("pull", "--ff-only", "--no-tags", "origin", args.branch, check=False)
        if pull.returncode != 0:
            failures.append(f"git pull --ff-only failed: {pull.stderr.strip()}")
            log(f"ERROR: {failures[-1]}")
        else:
            for path in allocated:
                content = Path(path).read_text()
                block = content[content.find(WORKFLOW_MARKER):] if WORKFLOW_MARKER in content else "(no block found!)"
                if WORKFLOW_MARKER not in content:
                    failures.append(f"{path}: no allocation block after pulling {args.branch}")
                log(f"--- {path} after pull:\n{block.rstrip()}")

    if failures:
        log(f"\n{len(failures)} failure(s):")
        for failure in failures:
            log(f"  - {failure}")
        return 1
    log(f"\nDone: {len(allocated)} allocated, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
