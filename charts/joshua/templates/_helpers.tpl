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

{{/* The image for one container. An empty tag means the chart's appVersion. */}}
{{- define "joshua.image" -}}
{{- $tag := .root.Values.image.tag | default .root.Chart.AppVersion -}}
{{- printf "%s/%s/joshua-ai-%s:%s" .root.Values.image.registry .root.Values.image.repository .name $tag -}}
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

{{/* One environment entry for each key a container reads from its Secret. */}}
{{- define "joshua.secretEnv" -}}
{{- $secret := .secret -}}
{{- range .keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ . }}
{{- end }}
{{- end -}}
