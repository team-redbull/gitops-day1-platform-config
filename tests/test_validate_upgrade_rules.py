"""Tests for scripts/validate_upgrade_rules.py.

The real upgrade/ladder.yaml must pass. Every other case builds a throwaway repo
in tmp_path, so the real sites/ and upgrade/ trees are never touched.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_upgrade_rules as rules  # noqa: E402

GOOD_LADDER = """\
schemaVersion: 1
maxWorkerSkew: 2
verifiedAgainstGraph: "2026-10-31"
release:
  imagePath: registry-internal/openshift-release
  repo: "{imagePath}/openshift/release-images"
  tag: "{version}-x86_64"
  cpoRepo: "{imagePath}/openshift/release"
  cpoTag: "{version}-x86_64-hypershift"
ladder:
  - { version: 4.19.53, must: true }
  - { version: 4.20.44, must: true, dataPlane: required }
  - { version: 4.21.40 }
"""

GOOD_MCE = """\
mce:
  version: 2.17.3
  supportedHostedVersions: ["4.22", "4.21", "4.20"]
maxConcurrentControlPlanes: 2
"""


def _repo(tmp_path, ladder=GOOD_LADDER, mces=None, mce_dirs=("ocp4-prep-mce-site1-a",)):
    (tmp_path / "upgrade" / "mces").mkdir(parents=True)
    (tmp_path / "upgrade" / "ladder.yaml").write_text(ladder)
    for name in mce_dirs:
        (tmp_path / "sites" / "site1" / "mces" / name).mkdir(parents=True)
    for name, text in (mces or {}).items():
        (tmp_path / "upgrade" / "mces" / f"{name}.yaml").write_text(text)
    return tmp_path


def _errors(tmp_path, **kw):
    return rules.validate_repo(_repo(tmp_path, **kw))


def test_the_real_rules_are_valid():
    assert rules.validate_repo(REPO_ROOT) == []


def test_a_good_ladder_and_mce_file_pass(tmp_path):
    assert _errors(tmp_path, mces={"ocp4-prep-mce-site1-a": GOOD_MCE}) == []


def test_main_exit_status(tmp_path, capsys):
    assert rules.main(["--repo-root", str(_repo(tmp_path))]) == 0
    bad = _repo(tmp_path / "bad", ladder=GOOD_LADDER.replace("4.20.44", "4.19.53"))
    assert rules.main(["--repo-root", str(bad)]) == 1
    assert "problem(s)" in capsys.readouterr().out


class TestLadder:
    def test_rungs_out_of_order_or_repeated(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace("4.21.40", "4.20.44"))
        assert any("strictly ascending" in e for e in errors)

    def test_a_minor_with_no_rung(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace("4.20.44", "4.19.60"))
        assert any("no rung for 4.20" in e for e in errors)

    @pytest.mark.parametrize("version", ["4.20.44-x86_64", "4.20", "5.0.1", "latest"])
    def test_a_version_that_is_not_a_bare_ocp4_release(self, tmp_path, version):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace("4.20.44", version))
        assert any("bare OCP 4 release" in e for e in errors)

    @pytest.mark.parametrize("skew", ["0", "4", "true", "two"])
    def test_max_worker_skew_outside_hypershifts_limit(self, tmp_path, skew):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace("maxWorkerSkew: 2", f"maxWorkerSkew: {skew}"))
        assert any("maxWorkerSkew" in e for e in errors)

    def test_an_unknown_key(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER + "imagePaths: {}\n")
        assert any("unknown key 'imagePaths'" in e for e in errors)

    def test_a_key_written_twice(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER + "maxWorkerSkew: 3\n")
        assert any("written twice" in e for e in errors)

    def test_data_plane_takes_only_required(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace("dataPlane: required", "dataPlane: optional"))
        assert any("dataPlane" in e for e in errors)

    def test_a_template_without_its_placeholder(self, tmp_path):
        errors = _errors(tmp_path, ladder=GOOD_LADDER.replace('"{version}-x86_64"', '"4.20.44-x86_64"'))
        assert any("release.tag must contain {version}" in e for e in errors)

    def test_an_unquoted_date_is_accepted(self, tmp_path):
        ladder = GOOD_LADDER.replace('"2026-10-31"', "2026-10-31")
        assert _errors(tmp_path, ladder=ladder) == []

    def test_a_null_date_is_accepted(self, tmp_path):
        ladder = GOOD_LADDER.replace('"2026-10-31"', "null")
        assert _errors(tmp_path, ladder=ladder) == []


class TestMceFiles:
    def test_a_file_for_an_mce_that_does_not_exist(self, tmp_path):
        errors = _errors(tmp_path, mces={"ocp4-prep-mce-typo": GOOD_MCE})
        assert any("no MCE named 'ocp4-prep-mce-typo'" in e for e in errors)

    def test_an_unquoted_minor_is_a_float(self, tmp_path):
        mce = GOOD_MCE.replace('["4.22", "4.21", "4.20"]', '["4.22", 4.20]')
        errors = _errors(tmp_path, mces={"ocp4-prep-mce-site1-a": mce})
        assert any("YAML float" in e for e in errors)

    def test_a_repeated_minor(self, tmp_path):
        mce = GOOD_MCE.replace('"4.21", "4.20"', '"4.21", "4.21"')
        errors = _errors(tmp_path, mces={"ocp4-prep-mce-site1-a": mce})
        assert any("lists 4.21 twice" in e for e in errors)

    def test_a_bad_mce_version_and_limit(self, tmp_path):
        mce = GOOD_MCE.replace("2.17.3", "4.20.1").replace("Planes: 2", "Planes: 0")
        errors = _errors(tmp_path, mces={"ocp4-prep-mce-site1-a": mce})
        assert any("mce.version" in e for e in errors)
        assert any("maxConcurrentControlPlanes" in e for e in errors)


class TestSchemasMatchTheValidator:
    """The validator's key sets and the editor schemas must never drift apart."""

    def _schema(self, name):
        return json.loads((REPO_ROOT / "upgrade" / "schema" / name).read_text())

    def test_ladder_schema(self):
        schema = self._schema("ladder.schema.json")
        assert set(schema["properties"]) == rules.LADDER_KEYS
        assert set(schema["required"]) == rules.LADDER_KEYS
        assert set(schema["properties"]["release"]["properties"]) == rules.RELEASE_KEYS
        assert set(schema["$defs"]["rung"]["properties"]) == rules.RUNG_KEYS
        assert schema["properties"]["maxWorkerSkew"]["maximum"] == rules.MAX_SKEW_LIMIT

    def test_mce_schema(self):
        schema = self._schema("mce.schema.json")
        assert set(schema["properties"]) == rules.MCE_FILE_KEYS
        assert set(schema["properties"]["mce"]["properties"]) == rules.MCE_KEYS
