# The Joshua chart

Joshua runs as three containers that share one volume. This chart installs
them, plus an optional Postgres with pgvector.

The chart names no deployment. Your host names, your storage, and your images
go in one values file of your own. Your credentials do not: make the Secrets
first, and name them in that file.

## Install

The chart ships as a package on each release. Take it from the release page,
then install it:

```
VERSION=0.0.6
curl -LO https://github.com/jakehigg/joshua-ai/releases/download/v$VERSION/joshua-$VERSION.tgz
helm install joshua ./joshua-$VERSION.tgz \
  --namespace joshua --create-namespace \
  --values my-values.yaml
```

Or install the chart straight from a checkout, which is what a deployment that
renders from git does:

```
helm install joshua ./charts/joshua \
  --namespace joshua --create-namespace \
  --values my-values.yaml
```

The chart is not in an OCI registry yet, so there is no `helm install
oci://...` for it. The images are, and the chart pulls them from there.

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
  --from-literal=DATABASE_URL=postgresql://joshua:YOUR_PASSWORD@joshua-postgres:5432/joshua \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN=... \
  --from-literal=POSTGRES_PASSWORD=YOUR_PASSWORD \
  --from-literal=JOSHUA_TOKEN_CORE=...
```

A private registry needs its own Secret in the same way, named in
`imagePullSecrets`:

```
kubectl create secret docker-registry joshua-registry -n joshua \
  --docker-server=registry.example.net --docker-username=... --docker-password=...
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

The database has the same two ways. `postgres.storage.existingClaim` binds a
claim you made yourself, and the StatefulSet then makes no claim of its own:

```yaml
postgres:
  storage:
    existingClaim: joshua-pgdata
```

Use it when you make the volumes outside the chart, because a volume lives
longer than a release. The claim must exist before the StatefulSet starts.
Without it, the StatefulSet makes the claim from `postgres.storage.size`,
`storageClass`, and `volumeName`.

`dataPermissions.enabled` runs a short init container that makes `/data` and its
two subdirectories writable for uid 1000. An NFS export usually arrives owned by
root, and Kubernetes does not apply `fsGroup` to an NFS volume. Leave it on
unless you know your storage arrives with the right owner.

## The viewer

The chart can also run the read-only web viewer, which is the gateway image
with a second entrypoint. It is off by default. A person signs in with HTTP
basic and reads the wiki, their own profile, their own journal, their own
attachments, and the shared profile. A member can move a wiki page to the
trash. The viewer holds no fleet token and no upstream credential; it reads
the volume and calls no container.

Two switches turn it on, and both are needed:

```yaml
# 1. put the pod in the cluster
viewer:
  enabled: true
  ingress:
    enabled: true
    className: nginx-internal
    host: <your host>
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-prod
    tls:
      enabled: true

# 2. let the process start, inside the joshua.yaml you pass as `config`
config: |
  ...
  viewer:
    enabled: true
    users:
      <person-id>: ""
```

With only the first, the container exits at once and its log says why.

The passwords are a Secret, named by `secrets.viewer` and made outside the
chart like every other. It holds one key, `VIEWER_PASSWORDS`, whose value is
`<person-id>=<bcrypt hash>` pairs, comma separated:

```
kubectl -n <namespace> create secret generic joshua-viewer-secrets \
  --from-literal=VIEWER_PASSWORDS='alice=$2b$12$...,bob=$2b$12$...'
```

`viewer.users` in your config is the authorization: a person who is not a key
there cannot sign in, whatever the Secret holds.

The viewer serves plain HTTP and puts every route behind HTTP basic, so the
password crosses the wire on each request. Terminate TLS at the ingress, and
keep the host off the public internet unless you mean to publish your notes.

## Enhancements

An enhancement is an optional component from another project, not from
Joshua. It runs someone else's code, in its own pod, under its own license and
its own repository. Every enhancement is off by default.

The wiki is a folder of Markdown files at `/data/wiki`. Any tool that reads a
folder of Markdown files can be its frontend. Each enhancement fills a
**slot**: a role such as `wiki`. `use` in that slot names the **choice**, the
frontend that fills it. `docs/enhancements.md` has the full contract for the
folder and lists what each choice may and may not touch.

### Otter Wiki

[Otter Wiki](https://github.com/redimp/otterwiki) is a wiki over a git
repository of Markdown files. Turn it on:

```yaml
enhancements:
  wiki:
    use: otterwiki
    otterwiki:
      ingress:
        enabled: true
        className: nginx
        host: notes.example.com
        annotations:
          cert-manager.io/cluster-issuer: letsencrypt-prod
        tls:
          enabled: true
      persistence:
        size: 1Gi
```

The pod mounts the wiki alone, at `/app-data/repository`, through a subPath
mount. It never sees a person's journal, profile, or attachments. A second
claim, `enhancements.wiki.otterwiki.persistence` (`joshua-otterwiki-data`
unless you set `existingClaim`), holds `/app-data`: its accounts, its
settings, and its search database, apart from the wiki.

Otter Wiki needs no Secret. It writes its own `SECRET_KEY` into
`/app-data/settings.cfg` on first start. Open the site and register an
account; the first account becomes the admin.

Otter Wiki runs `git init` in the wiki on first start, so a `.git` directory
appears in `wiki/`. Joshua ignores dot-directories.

Otter Wiki serves plain HTTP behind its own login, and the session cookie
crosses the wire on each request. Terminate TLS at the ingress, and keep the
host off the public internet unless you mean to publish your notes.

The [Otter Wiki documentation](https://otterwiki.com/Configuration) is the
reference for every setting it reads. `docs/enhancements.md` has the Docker
Compose side of this same choice, and the folder contract every wiki frontend
follows.

## What the chart does not expose

Only `channels` has an Ingress, and only when you enable it. `core` holds the
agent and `gateway` holds every upstream credential, so neither is reachable
from outside the namespace.

`channels` needs an Ingress only when a channel gets a message pushed to it,
such as the iMessage webhook from BlueBubbles. Telegram polls out and needs
none.

```yaml
ingress:
  enabled: true
  className: nginx
  host: joshua.example.net
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    enabled: true
    # Empty means "<host>-tls".
    secretName: joshua-channels-tls
```

The chart stops with an error when `ingress.enabled` is true and
`ingress.host` is empty, because an Ingress with no host takes every request
that reaches the class. One host belongs to one Ingress: the admission webhook of
the controller refuses a second Ingress with the same host and path.

## Images

The three images share a tag, because they ship together. An empty
`image.tag` means the chart's `appVersion`. The chart builds each reference as
`<registry>/<repository>/joshua-ai-<component>:<tag>`.

Set `core.image`, `channels.image`, or `gateway.image` to a whole reference to
ignore that block for one container, such as a build in your own registry:

```yaml
core:
  image: registry.example.net/joshua/core:local-1
```

A package on a registry can be private, and that is a different setting from
the visibility of the repository. Check the package page after the first
release. When the package is private, name a pull secret in
`imagePullSecrets`.

## Run main, or a branch

A push to any branch, `main` included, builds the three images for amd64 and
tags them with the full commit SHA, as
`<registry>/<repository>/joshua-ai-<component>:<sha>`. A second tag,
`branch-<name>`, moves with each push, so `branch-main` is always the newest
commit on `main`. A branch build is not a release: it has no release page and
no packaged chart.

To follow `main` with ArgoCD, point the chart source at `main` and take the
image tag from the commit ArgoCD resolved:

```yaml
spec:
  sources:
    - repoURL: https://github.com/jakehigg/joshua-ai.git
      path: charts/joshua
      targetRevision: main
      helm:
        parameters:
          - name: image.tag
            value: $ARGOCD_APP_REVISION
```

A feature branch works the same way: set `targetRevision` to its name. Each
push then renders the chart of the new commit with the images of the same
commit. ArgoCD can see a commit before its build ends. The new pod waits in
`ImagePullBackOff` until the images arrive, and a container with the
`Recreate` strategy is down for that time. Use this on an instance that you
test, not on one that people use.

Without ArgoCD, the simple mutable option is `image.tag: branch-main`. It
moves to the newest commit on `main` at your next `helm upgrade`, with no
parameter to compute.

The chart version on `main` still carries the last release's version, while
its templates are newer. An empty `image.tag` against a chart from `main`
pulls that release's images, which may not match your templates. A release
tag is the only place the chart and its images are guaranteed to agree.

## Versions

The chart version and the application version are always the same. The chart
and the three containers ship together, and an interface between them can
change in any release before 1.0, so a chart must never meet an image it did
not ship with.

This makes an install safe by default: leave `image.tag` empty and the chart
pulls the images of its own version. Set `image.tag` only to pin a build you
made yourself, and then keep the chart at the version those images came from.
