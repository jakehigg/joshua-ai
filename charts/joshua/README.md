# The Joshua chart

Joshua runs as three containers that share one volume. This chart installs
them, plus an optional Postgres with pgvector.

The chart names no deployment. Your host names, your storage, and your secrets
go in your own values file.

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

**The three Secrets.** The chart never makes a Secret, because a secret in a
values file is a secret in a Git repository. Make them with Sealed Secrets,
External Secrets, SOPS, or by hand, and name them under `secrets`.

| Secret | Keys |
|---|---|
| `secrets.core` | `DATABASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, `POSTGRES_PASSWORD` when the chart runs Postgres, and every `JOSHUA_TOKEN_*` |
| `secrets.channels` | `TELEGRAM_BOT_TOKEN`, `BLUEBUBBLES_PASSWORD`, `IMESSAGE_WEBHOOK_SECRET`, and every `JOSHUA_TOKEN_*` |
| `secrets.gateway` | Every `JOSHUA_TOKEN_*`, and one key for each upstream credential an MCP server reads |

`make init-env` in the repository mints the `JOSHUA_TOKEN_*` values.

## The volume

All three containers mount `/data` at once, so `persistence.accessMode` is
`ReadWriteMany` and the storage class must support it. To bind one named
volume, set `persistence.volumeName` and `persistence.storageClass: ""`: an
empty string disables dynamic provisioning.

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
`image.tag` means the chart's `appVersion`.

A package on a registry can be private, and that is a different setting from
the visibility of the repository. Check the package page after the first
release. When the package is private, name a pull secret in
`imagePullSecrets`.
