#!/usr/bin/env python3
"""
Validate the hosted-cluster upgrade rules without changing anything.

Checks upgrade/ladder.yaml and every upgrade/mces/<mce>.yaml against their
schemas (upgrade/schema/) and against the rules a schema cannot express:

  * ladder rungs are bare X.Y.Z OCP 4 releases, strictly ascending, no repeats;
  * every minor between the lowest and the highest rung has at least one rung —
    the upgrade workflow moves the control plane one minor at a time and refuses
    to enter a minor nobody verified;
  * maxWorkerSkew is 1..3 (HyperShift allows NodePools 3 minors behind);
  * adminAcks name each gate once, in the CVO's own `ack-4.<minor>-...` form,
    each with the APIRequestCount names of the APIs it covers and its source;
    apiUsageIgnoredUsers lists each user once;
  * an MCE file is named after an MCE that exists under sites/*/mces/, and its
    supportedHostedVersions are quoted "4.N" strings, without repeats.

YAML pitfalls are refused rather than tolerated: a key written twice (PyYAML
would silently keep the last one) and an unquoted minor like 4.20 (a float).

Only the standard library and PyYAML, like every script here.

Usage:
    python scripts/validate_upgrade_rules.py [--repo-root PATH]

Exit status 0 when everything is valid, 1 with one line per problem otherwise.
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

LADDER_FILE = "upgrade/ladder.yaml"
MCE_GLOB = "upgrade/mces/*.yaml"
MCE_DIRS_GLOB = "sites/*/mces/*"

# Keep these in step with upgrade/schema/*.json (tests/test_validate_upgrade_rules.py
# fails when they drift).
LADDER_REQUIRED = {"schemaVersion", "maxWorkerSkew", "verifiedAgainstGraph", "release", "ladder"}
LADDER_KEYS = LADDER_REQUIRED | {"adminAcks", "apiUsageIgnoredUsers"}
RELEASE_KEYS = {"imagePath", "repo", "tag", "cpoRepo", "cpoTag"}
RUNG_KEYS = {"version", "must", "dataPlane"}
MCE_FILE_KEYS = {"mce", "maxConcurrentControlPlanes"}
MCE_KEYS = {"version", "supportedHostedVersions"}
ADMIN_ACK_KEYS = {"gate", "source", "apis"}

# HyperShift supports NodePools up to 3 minors behind their HostedCluster.
MAX_SKEW_LIMIT = 3

OCP_RELEASE = re.compile(r"4\.(\d+)\.(\d+)")
OCP_MINOR = re.compile(r"4\.(\d+)")
MCE_VERSION = re.compile(r"2\.\d+\.\d+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# An admin gate, as the CVO matches it ("^ack-[4-5][.]([0-9]{1,})-[^-]",
# cluster-version-operator pkg/cvo/upgradeable.go), narrowed to OCP 4 and to
# the lowercase key names Red Hat uses.
ADMIN_GATE = re.compile(r"ack-4\.\d+-[a-z0-9][a-z0-9.-]*")
# An APIRequestCount name: <resource>.<version>[.<group>], e.g.
# validatingwebhookconfigurations.v1beta1.admissionregistration.k8s.io.
API_REQUEST_COUNT = re.compile(r"[a-z0-9-]+\.v\d+[a-z0-9]*(\.[a-z0-9-]+)*")


class _NoDuplicatesLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping key written twice."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"key {key!r} is written twice", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_NoDuplicatesLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_NoDuplicatesLoader)


def _keys(errors, where, mapping, allowed, required):
    if not isinstance(mapping, dict):
        errors.append(f"{where}: must be a mapping")
        return False
    for key in sorted(set(mapping) - allowed):
        errors.append(f"{where}: unknown key {key!r}")
    for key in sorted(required - set(mapping)):
        errors.append(f"{where}: missing key {key!r}")
    return True


def _release_key(version):
    match = OCP_RELEASE.fullmatch(version)
    return (int(match.group(1)), int(match.group(2)))


def validate_ladder(data, where=LADDER_FILE):
    """Problems in the ladder file's content, as a list of messages."""
    errors = []
    if not _keys(errors, where, data, LADDER_KEYS, LADDER_REQUIRED):
        return errors

    if data.get("schemaVersion") != 1:
        errors.append(f"{where}: schemaVersion must be 1")

    skew = data.get("maxWorkerSkew")
    if isinstance(skew, bool) or not isinstance(skew, int) or not 1 <= skew <= MAX_SKEW_LIMIT:
        errors.append(
            f"{where}: maxWorkerSkew must be an integer from 1 to {MAX_SKEW_LIMIT} "
            f"(HyperShift's limit), got {skew!r}"
        )

    verified = data.get("verifiedAgainstGraph")
    if isinstance(verified, datetime.date):
        verified = verified.isoformat()  # an unquoted date; accepted, written back quoted
    if verified is not None and not (isinstance(verified, str) and DATE.fullmatch(verified)):
        errors.append(f"{where}: verifiedAgainstGraph must be YYYY-MM-DD or null, got {verified!r}")

    release = data.get("release")
    if _keys(errors, f"{where}: release", release, RELEASE_KEYS, RELEASE_KEYS):
        for key in sorted(RELEASE_KEYS & set(release)):
            if not isinstance(release[key], str) or not release[key].strip():
                errors.append(f"{where}: release.{key} must be a non-empty string")
        for key, placeholder in (("repo", "{imagePath}"), ("cpoRepo", "{imagePath}"),
                                 ("tag", "{version}"), ("cpoTag", "{version}")):
            if isinstance(release.get(key), str) and placeholder not in release[key]:
                errors.append(f"{where}: release.{key} must contain {placeholder}")

    errors += validate_admin_acks(data, where)

    rungs = data.get("ladder")
    if not isinstance(rungs, list) or not rungs:
        errors.append(f"{where}: ladder must be a non-empty list")
        return errors

    versions = []
    for index, rung in enumerate(rungs):
        at = f"{where}: ladder[{index}]"
        if not _keys(errors, at, rung, RUNG_KEYS, {"version"}):
            continue
        version = rung.get("version")
        if not isinstance(version, str) or not OCP_RELEASE.fullmatch(version):
            errors.append(f"{at}.version must be a bare OCP 4 release like 4.20.44, got {version!r}")
        else:
            versions.append((index, version))
        if "must" in rung and not isinstance(rung["must"], bool):
            errors.append(f"{at}.must must be true or false")
        if "dataPlane" in rung and rung["dataPlane"] != "required":
            errors.append(f"{at}.dataPlane can only be 'required'")

    for (_, earlier), (index, later) in zip(versions, versions[1:]):
        if _release_key(later) <= _release_key(earlier):
            errors.append(
                f"{where}: ladder[{index}] {later} is not newer than {earlier}: "
                "rungs must be strictly ascending, without repeats"
            )

    minors = {_release_key(v)[0] for _, v in versions}
    if minors:
        for minor in range(min(minors), max(minors) + 1):
            if minor not in minors:
                errors.append(
                    f"{where}: no rung for 4.{minor}; the control plane moves one minor "
                    "at a time, so every minor between the lowest and highest rung needs one"
                )
    return errors


def _unique_strings(errors, at, values, pattern=None, what="a non-empty string"):
    """Each item a string (matching `pattern`, when given), none repeated."""
    if not isinstance(values, list) or not values:
        errors.append(f"{at} must be a non-empty list")
        return
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip() or (pattern and not pattern.fullmatch(value)):
            errors.append(f"{at}[{index}] must be {what}, got {value!r}")
    strings = [v for v in values if isinstance(v, str)]
    for value in sorted({v for v in strings if strings.count(v) > 1}):
        errors.append(f"{at} lists {value} twice")


def validate_admin_acks(data, where=LADDER_FILE):
    """Problems in the optional adminAcks and apiUsageIgnoredUsers, as a list of messages."""
    errors = []
    if "apiUsageIgnoredUsers" in data:
        _unique_strings(errors, f"{where}: apiUsageIgnoredUsers", data["apiUsageIgnoredUsers"])
    if "adminAcks" not in data:
        return errors
    acks = data["adminAcks"]
    if not isinstance(acks, list) or not acks:
        errors.append(f"{where}: adminAcks must be a non-empty list (leave the key out for none)")
        return errors
    gates = []
    for index, ack in enumerate(acks):
        at = f"{where}: adminAcks[{index}]"
        if not _keys(errors, at, ack, ADMIN_ACK_KEYS, ADMIN_ACK_KEYS):
            continue
        gate = ack.get("gate")
        if not isinstance(gate, str) or not ADMIN_GATE.fullmatch(gate):
            errors.append(
                f"{at}.gate must be an admin gate like "
                f"ack-4.19-admissionregistration-v1beta1-api-removals-in-4.20, got {gate!r}"
            )
        else:
            gates.append(gate)
        if not isinstance(ack.get("source"), str) or not ack["source"].strip():
            errors.append(f"{at}.source must say where the API list comes from (a Red Hat article)")
        _unique_strings(
            errors, f"{at}.apis", ack.get("apis"), API_REQUEST_COUNT,
            "an APIRequestCount name like validatingwebhookconfigurations.v1beta1.admissionregistration.k8s.io",
        )
    for gate in sorted({g for g in gates if gates.count(g) > 1}):
        errors.append(f"{where}: adminAcks lists {gate} twice")
    return errors


def validate_mce(data, name, known_mces, where):
    """Problems in one upgrade/mces/<name>.yaml, as a list of messages."""
    errors = []
    if name not in known_mces:
        errors.append(f"{where}: no MCE named {name!r} under sites/*/mces/")
    if not _keys(errors, where, data, MCE_FILE_KEYS, MCE_FILE_KEYS):
        return errors

    limit = data.get("maxConcurrentControlPlanes")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        errors.append(f"{where}: maxConcurrentControlPlanes must be an integer of at least 1")

    mce = data.get("mce")
    if not _keys(errors, f"{where}: mce", mce, MCE_KEYS, MCE_KEYS):
        return errors
    version = mce.get("version")
    if not isinstance(version, str) or not MCE_VERSION.fullmatch(version):
        errors.append(f"{where}: mce.version must be an MCE release like 2.17.3, got {version!r}")

    supported = mce.get("supportedHostedVersions")
    if not isinstance(supported, list) or not supported:
        errors.append(f"{where}: mce.supportedHostedVersions must be a non-empty list")
        return errors
    for index, minor in enumerate(supported):
        at = f"{where}: mce.supportedHostedVersions[{index}]"
        if isinstance(minor, float):
            errors.append(f"{at}: {minor!r} is a YAML float; quote it, e.g. \"4.20\"")
        elif not isinstance(minor, str) or not OCP_MINOR.fullmatch(minor):
            errors.append(f"{at}: must be an OCP minor like \"4.20\", got {minor!r}")
    strings = [m for m in supported if isinstance(m, str)]
    for minor in sorted({m for m in strings if strings.count(m) > 1}):
        errors.append(f"{where}: mce.supportedHostedVersions lists {minor} twice")
    return errors


def validate_repo(repo_root):
    """Every problem in the repo's upgrade rules, as a list of messages."""
    repo_root = Path(repo_root)
    errors = []

    ladder_path = repo_root / LADDER_FILE
    if not ladder_path.is_file():
        return [f"{LADDER_FILE}: missing"]
    try:
        errors += validate_ladder(load_yaml(ladder_path))
    except yaml.YAMLError as err:
        errors.append(f"{LADDER_FILE}: not valid YAML: {err}")

    known_mces = {path.name for path in repo_root.glob(MCE_DIRS_GLOB) if path.is_dir()}
    for path in sorted(repo_root.glob(MCE_GLOB)):
        where = str(path.relative_to(repo_root))
        try:
            errors += validate_mce(load_yaml(path), path.stem, known_mces, where)
        except yaml.YAMLError as err:
            errors.append(f"{where}: not valid YAML: {err}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    errors = validate_repo(args.repo_root)
    for message in errors:
        print(message, flush=True)
    if errors:
        print(f"upgrade rules: {len(errors)} problem(s)", flush=True)
        return 1
    print("upgrade rules: valid", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
