# The Joshua chart

Joshua runs as three containers that share one volume. This chart installs
them, plus an optional Postgres with pgvector.

The chart names no deployment. Your host names, your storage, and your secrets
go in your own values file, and one file is enough: the config, the
credentials, the registry login, and the volume all have a value here.
`ci/single-file-values.yaml` is a whole installation in one file, with no real
credential in it.

## Install

```
helm install joshua oci://ghcr.io/jakehigg/charts/joshua \
  --namespace joshua --create-namespace \
  --values my-values.yaml
```

## What you must set

**`config`** is the whole `joshua.yaml`, as a string. Every option is in
[docs/config.md](../../docs/config.md). The chart writes it to a ConfigMap and
mounts it read-only in all three containers. Set `existingConfigMap` instead to
use a ConfigMap you made yourself, under the key `joshua.yaml`.

**The Secrets.** Every credential comes through a `secretKeyRef`; the chart
holds no other path to one. You have two ways to supply them.

*Refer to Secrets you made.* This is the default, and the right way for a chart
that ArgoCD reads from Git, because a secret in a values file is a secret in
that repository. Make them with Sealed Secrets, External Secrets, SOPS, or by
hand, and name them under `secrets`.

*Let the chart make them.* Set `secrets.create: true` and put the credentials
in the `values` map of each block. One file then holds the whole installation.
Keep that file out of Git: `helm install -f my-values.yaml` reads it from your
disk, and Helm also stores the rendered credentials in the release Secret in
the cluster.

```yaml
secrets:
  create: true
  core:
    values:
      DATABASE_URL: postgresql://joshua:pw@joshua-postgres:5432/joshua
      CLAUDE_CODE_OAUTH_TOKEN: sk-...
  gateway:
    values:
      SPOTIFY_TOKEN: ...        # an MCP server reads it as ${SPOTIFY_TOKEN}
```

A key in `values` needs no entry in `keys`: the chart passes it to the
container under its own name. Two blocks that share one `name` become one
Secret, as `core` and `postgres` do by default.

Each block gives a default Secret in `name` and the variables in `keys`:

| Block | Default keys |
|---|---|
| `secrets.core` | `DATABASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, and every `JOSHUA_TOKEN_*` |
| `secrets.channels` | `TELEGRAM_BOT_TOKEN`, `BLUEBUBBLES_PASSWORD`, `IMESSAGE_WEBHOOK_SECRET`, and every `JOSHUA_TOKEN_*` |
| `secrets.gateway` | Every `JOSHUA_TOKEN_*`, and one key for each upstream credential an MCP server reads |
| `secrets.postgres` | `POSTGRES_PASSWORD`, when the chart runs Postgres |

An empty value takes the variable from `name`, under a key of the same
spelling. Give `name` or `key` to take one variable from somewhere else:

```yaml
secrets:
  core:
    name: joshua-core-secrets
    keys:
      DATABASE_URL: {}
      CLAUDE_CODE_OAUTH_TOKEN: {name: anthropic-oauth, key: token}
```

Set a key to `null` to stop passing that variable, because Helm merges a map
and does not replace it. Add a key to pass another variable, such as an
upstream credential an MCP server reads as `${VAR}`. Every variable is optional
unless the key says `{optional: false}`, so a container starts without a
credential it does not use.

`make init-env` in the repository mints the `JOSHUA_TOKEN_*` values.

## The volume

All three containers mount `/data` at once, so `persistence.accessMode` is
`ReadWriteMany` and the storage class must support it. By default the chart
makes a claim and the cluster provisions the storage.

For storage that the cluster's provisioner does not make, you have two ways:

- **Bind storage you made yourself.** Set `persistence.existingClaim` to your
  claim, or `persistence.volumeName` to your PersistentVolume with
  `persistence.storageClass: ""`, because an empty string stops dynamic
  provisioning.
- **Let the chart make the PersistentVolume.** Set
  `persistence.volume.enabled` and put the volume source in
  `persistence.volume.source`, which the chart copies into the
  PersistentVolume as it is. The claim then binds to it by name.

```yaml
persistence:
  storageClass: ""
  volume:
    enabled: true
    name: joshua-data-nfs
    capacity: 200Gi
    reclaimPolicy: Retain
    mountOptions: [nfsvers=4.1]
    source:
      nfs:
        server: 10.0.0.2
        path: /export/joshua
```

The PersistentVolume carries `helm.sh/resource-policy: keep`, so an uninstall
leaves the data. `postgres.storage.volumeName` binds the database to a named
volume in the same way.

`dataPermissions.enabled` runs a short init container that makes `/data` and its
two subdirectories writable for uid 1000. An NFS export usually arrives owned by
root, and Kubernetes does not apply `fsGroup` to an NFS volume. Leave it on
unless you know your storage arrives with the right owner.

## What the chart does not expose

Only `channels` has an Ingress, and only when you enable it. `core` holds the
agent and `gateway` holds every upstream credential, so neither is reachable
from outside the namespace.

## Images

The three images share a tag, because they ship together. An empty
`image.tag` means the chart's `appVersion`. The chart builds each reference as
`<registry>/<repository>/joshua-ai-<component>:<tag>`.

Set `core.image`, `channels.image`, or `gateway.image` to a whole reference to
ignore that block for one container, such as a build in your own registry:

```yaml
core:
  image: harbor.example.net/joshua/core:local-1
```

A package on a registry can be private, and that is a different setting from
the visibility of the repository. Check the package page after the first
release. When the package is private, name a pull secret in
`imagePullSecrets`, or let the chart make one and add it for you:

```yaml
imagePullSecret:
  create: true
  registry: harbor.example.net
  username: robot$joshua
  password: ...
```
