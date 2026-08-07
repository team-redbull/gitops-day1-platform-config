# gitops-day1-platform-config

The values repo for the hosted-cluster deployment. GitHub mirror of the
`gitops-day1/platform-config` project in the air-gapped GitLab.

Argo CD never renders anything from here directly — this repo is only ever a
`ref: values` source. The charts live in
[`gitops-day1-argocd-platform`](https://github.com/team-redbull/gitops-day1-argocd-platform)
(the ApplicationSet cascade) and
[`helm-charts-hostedclusters-setup`](https://github.com/team-redbull/helm-charts-hostedclusters-setup)
(what a hosted cluster actually gets).

## Layout

```
sites/
  configValues.yaml                                    # global, every cluster
  <site>/
    values.yaml                                        # site-wide
    mces/<mce>/
      values.yaml                                      # MCE-wide
      hostedClusters/<cluster>.yaml                    # one file per cluster
```

Those four files are the merge order Argo CD hands to Helm, last value wins,
layered on top of the chart's own `values.yaml`.

## `dhcp_values`

The only key this repo owns. `dhcp_api` and `crossplane` are chart-owned: one
API per cluster is a platform constant, not per-cluster config.

Field reference: [`docs/dhcp_values.md`](https://github.com/team-redbull/dhcp_scope_manager/blob/main/docs/dhcp_values.md)
in `dhcp_scope_manager`, which also holds the CI validator:

```bash
python3 scripts/validate_dhcp_values.py --sites-dir <this repo>/sites
```

Two merge behaviours worth knowing before editing:

- **Lists are replaced, not appended.** A site file setting `dns.servers`
  discards the globals rather than extending them — use `dns.extraServers`.
- **Mappings deep-merge.** A cluster file's `failover` block only needs the
  fields that differ from `configValues.yaml`.

## Adding a cluster

Drop a `<cluster>.yaml` under the right MCE. A file with no `dhcp_values` block
renders no DHCP scope, which is the correct outcome for a cluster that does not
need one.
