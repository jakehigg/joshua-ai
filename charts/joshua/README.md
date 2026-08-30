# The Joshua chart

Joshua runs as three containers that share one volume. This chart installs
them, plus an optional Postgres with pgvector.

The chart names no deployment. Your host names, your storage, and your images
go in one values file of your own. Your credentials do not: make the Secrets
first, and name them in that file.

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

**The Secrets.** Make them before you install. The chart makes no Secret,
because a secret in a values file is a secret in each repository and each
backup that file reaches. Make them with Sealed Secrets, External Secrets,
SOPS, or by hand, and name them under `secrets`. Every credential comes through
a `secretKeyRef`; the chart holds no other path to one.

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

`make init-env` in the repository mints the `JOSHUA_TOKEN_*` values. To make
the Secrets by hand:

```
kubectl create secret generic joshua-core-secrets -n joshua \
  --from-literal=DATABASE_URL=postgresql://joshua:PW@joshua-postgres:5432/joshua \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN=... \
  --from-literal=POSTGRES_PASSWORD=PW \
  --from-literal=JOSHUA_TOKEN_CORE=...
```

A private registry needs its own Secret in the same way, named in
`imagePullSecrets`:

```
kubectl create secret docker-registry joshua-registry -n joshua \
  --docker-server=harbor.example.net --docker-username=... --docker-password=...
```

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
`imagePullSecrets`.
