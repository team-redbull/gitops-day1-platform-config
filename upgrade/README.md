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
| `release.imagePath` | The one full-mirror endpoint (or, with `mirrored: false`, the public source). Every hosted cluster's `imagePath` (and `imageHc`) must equal it |
| `release.repo`, `tag` | How a release image reference is built, matching the `hostedclusters-setup` chart (`<imagePath>/<releaseRepo>:<tag>`) |
| `release.cpoRepo`, `cpoTag` | The control-plane-operator image, matching the chart's `cpoRepo`. Both or neither; required while `mirrored` is true. Without them the payload's own CPO is used and nothing extra is pinned |
| `release.mirrored` | `false` when releases come straight from their public source (a connected lab): there is no IDMS to check. Absent means `true`, as in production |
| `ladder[]` | Rungs, ascending, at least one per minor between the lowest and the highest |
| `ladder[].version` | A bare release, `4.20.44` (no `-x86_64`) |
| `ladder[].must` | Never skipped, even in the target's minor |
| `ladder[].dataPlane: required` | Every NodePool rolls to this release before the control plane moves past it |
| `adminAcks[]` | Optional. Admin acks the workflow may give by itself (below) |
| `adminAcks[].gate` | The key the hosted cluster's `openshift-config-managed/admin-gates` names, `ack-4.19-admissionregistration-v1beta1-api-removals-in-4.20` |
| `adminAcks[].source` | Where the API list comes from: Red Hat's "Preparing to upgrade to OpenShift Container Platform 4.N" article |
| `adminAcks[].apis` | The APIs the gate covers, as APIRequestCount names: `<resource>.<version>.<group>` |
| `apiUsageIgnoredUsers` | Optional. Callers that do not count as using a gated API, because they walk every API (Red Hat KCS 6955985) |

How a path is built from it: the control plane first goes to its own minor's
rung, then rung by rung, one minor at a time, to the target. NodePools move only
when they would otherwise fall more than `maxWorkerSkew` minors behind, at a
`dataPlane: required` rung, and at the end. A minor with no rung cannot be
entered.

**Admin acks.** Some releases make an administrator acknowledge an API removal
before the control plane may leave their minor: 4.19 asks for
`ack-4.19-admissionregistration-v1beta1-api-removals-in-4.20` before 4.20. Until
the key is `"true"` in the hosted cluster's `openshift-config/admin-acks`, its
CVO reports `Upgradeable=False` (`AdminAckRequired`) and HyperShift refuses the
minor step. Right before such a step the workflow reads the hosted cluster's
`APIRequestCount` for the gate's `apis`:

- nobody outside `apiUsageIgnoredUsers` called them in the last 24 hours: the
  workflow writes the ack itself;
- somebody did: it lists them (user, user agent, verbs, request count) and waits
  for a person to approve the ack, or to retry once the clients are fixed;
- the gate is not listed here: it always waits for a person.

APIRequestCount cannot see idle workloads or tools that run less than once a
day (KCS 6955985), so listing a gate here is a team decision, reviewed like any
other change to this file. To add a gate, copy its API list from Red Hat's
article for that release.

**Adding a rung.** Pick a release with an upgrade edge FROM the previous rung and
TO the next one (check Red Hat's graph), mirror it, add it, and update
`verifiedAgainstGraph`.

**This copy is the sandbox's.** Its `release` block points at
`quay.io/openshift-release-dev/ocp-release` with `mirrored: false` and no CPO
image; production keeps `registry-internal/openshift-release` with the CPO pair
(commented in `ladder.yaml`).

## `mces/<mce>.yaml`

The file name is the MCE's name, as under `sites/*/mces/`.

| Key | Meaning |
|---|---|
| `mce.version` | The MultiClusterEngine version, `2.17.3` |
| `mce.supportedHostedVersions` | OCP minors its HyperShift operator can host, **quoted** (`"4.22"`; an unquoted `4.20` is a YAML float). Must equal the `hypershift/supported-versions` ConfigMap on that MCE, which the workflow checks before every run |
| `maxConcurrentControlPlanes` | How many hosted control planes on this MCE may upgrade at the same time |

## `upgrade:` in a cluster file

A cluster file (`sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml`) may
carry an `upgrade:` block. Only the file's own block counts, not one in a site
or MCE layer. The hostedclusters-setup chart never reads it. Every key is
optional; an unknown key or a bad value fails the run's planning, saying
which key.

```yaml
upgrade:
  profile: zoned                       # generic | zoned
  nodePoolOrder: [zone-a, zone-b, zone-c]
  soakMinutes: 30
  wave: 2
```

| Key | Meaning |
|---|---|
| `profile` | How the NodePools roll: `generic` or `zoned`. Default: `zoned` when any NodePool has a `topology.kubernetes.io/zone` label, else `generic` |
| `nodePoolOrder` | The NodePools in the order they roll, each once. Default: by zone, else file order. A run's own `nodepool_order` wins |
| `soakMinutes` | How long a NodePool's gate must hold before the next one may roll. Default: 30 for `zoned`, 0 for `generic` |
| `wave` | A whole number: `0` a lab cluster, `1` a canary, `2` and up production. No `wave` counts as production |

**What the wave allows.** Only waves 0 and 1 may run without a person at the
two optional stops. A run asked for `auto_approve: true` (it approves its own
plan) or `nodepool_mode: auto` (each NodePool rolls after the previous one's
soak, without the customer's `continue`) fails planning anywhere else, saying
why. Waves 0 and 1 are also where the upgrade agent will run a known-safe fix
itself once its tier 1 exists; today it proposes every fix, in every wave. Wave
0 alone goes on without `confirm-day2` after a control-plane step, and only
when the cluster has no day2 Application on its MCE (nothing to sync). No wave
skips an admin ack a client still needs, or any stop that needs a person. The workflow's docs page has the
whole table: `docs/site/cluster-upgrade/upgrade-cluster.html#modes` in
`team-redbull/workflows`.
