{{/* The release name, cut to the 63 characters a label allows. */}}
{{- define "joshua.fullname" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels every object carries. */}}
{{- define "joshua.labels" -}}
app.kubernetes.io/part-of: joshua
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/* The image for one container. The component's own `image` value wins as a
     whole reference, such as a build in a local registry. Without it the chart
     builds the reference from the shared block, where an empty tag means the
     chart's appVersion. */}}
{{- define "joshua.image" -}}
{{- $override := (index .root.Values .name).image | default "" -}}
{{- if $override -}}
{{- $override -}}
{{- else -}}
{{- $tag := .root.Values.image.tag | default .root.Chart.AppVersion -}}
{{- printf "%s/%s/joshua-ai-%s:%s" .root.Values.image.registry .root.Values.image.repository .name $tag -}}
{{- end -}}
{{- end -}}

{{/* The ConfigMap that holds joshua.yaml. */}}
{{- define "joshua.configMapName" -}}
{{- if .Values.existingConfigMap -}}
{{- .Values.existingConfigMap -}}
{{- else -}}
joshua-config
{{- end -}}
{{- end -}}

{{/* The security context every application container runs under. The agent has
     no shell and no file tools; this keeps the container itself as small a
     target. */}}
{{- define "joshua.containerSecurityContext" -}}
runAsUser: 1000
runAsGroup: 1000
runAsNonRoot: true
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
{{- end -}}

{{/* One environment entry for each secret a container reads. The entry takes
     its Secret from the key's own `name`, or from the component default, and
     its key from the key's own `key`, or from the variable name. An entry is
     optional unless the key sets `optional: false`, so a container starts
     without a credential it does not use. */}}
{{- define "joshua.secretEnv" -}}
{{- $default := .secret.name -}}
{{- range $var, $from := .secret.keys }}
{{- $from = $from | default dict }}
- name: {{ $var }}
  valueFrom:
    secretKeyRef:
      name: {{ $from.name | default $default }}
      key: {{ $from.key | default $var }}
      optional: {{ if hasKey $from "optional" }}{{ $from.optional }}{{ else }}true{{ end }}
{{- end }}
{{- end -}}
