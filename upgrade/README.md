# upgrade/ — hosted-cluster upgrade rules

Read by the cluster-upgrade workflow (`team-redbull/workflows`, domain
`cluster_upgrade`) at the start of every run. Nothing here is rendered by Argo CD:
the ApplicationSets and every day1 CI script only look under `sites/`.

```
upgrade/
  ladder.yaml            # fleet-wide: the releases the control plane steps through
  mces/<mce>.yaml        # per MCE: what its HyperShift can host, how many upgrade at once
  schema/                # JSON schemas, for editors (yaml-language-server)
```

Validate before you push:

```bash
python3 scripts/validate_upgrade_rules.py
```

## `ladder.yaml`

| Key | Meaning |
|---|---|
| `schemaVersion` | `1` |
| `maxWorkerSkew` | How many minors NodePools may lag their control plane. Team rule `2`; never above `3`, HyperShift's own limit |
| `verifiedAgainstGraph` | `"YYYY-MM-DD"` the rungs were last checked against the upgrade graph, or `null`. Shown to the approver |
| `release.imagePath` | The one full-mirror endpoint. Every hosted cluster's `imagePath` (and `imageHc`) must equal it |
| `release.repo`, `tag`, `cpoRepo`, `cpoTag` | How a release image reference is built, matching the `hostedclusters-setup` chart |
| `ladder[]` | Rungs, ascending, at least one per minor between the lowest and the highest |
| `ladder[].version` | A bare release, `4.20.44` (no `-x86_64`) |
| `ladder[].must` | Never skipped, even in the target's minor |
| `ladder[].dataPlane: required` | Every NodePool rolls to this release before the control plane moves past it |

How a path is built from it: the control plane first goes to its own minor's
rung, then rung by rung, one minor at a time, to the target. NodePools move only
when they would otherwise fall more than `maxWorkerSkew` minors behind, at a
`dataPlane: required` rung, and at the end. A minor with no rung cannot be
entered.

**Adding a rung.** Pick a release with an upgrade edge FROM the previous rung and
TO the next one (check Red Hat's graph), mirror it, add it, and update
`verifiedAgainstGraph`.

## `mces/<mce>.yaml`

The file name is the MCE's name, as under `sites/*/mces/`.

| Key | Meaning |
|---|---|
| `mce.version` | The MultiClusterEngine version, `2.17.3` |
| `mce.supportedHostedVersions` | OCP minors its HyperShift operator can host, **quoted** (`"4.22"`; an unquoted `4.20` is a YAML float). Must equal the `hypershift/supported-versions` ConfigMap on that MCE, which the workflow checks before every run |
| `maxConcurrentControlPlanes` | How many hosted control planes on this MCE may upgrade at the same time |
