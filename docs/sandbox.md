# The code sandbox

The sandbox lets Joshua run Python. Joshua uses it to count, filter, sort,
compare, and chart data, so a number that comes from a file comes from code,
not from a read of the file. Example: two exports of one reading list, and the
question "which book is in one list and not in the other?".

The sandbox is off by default. It is a separate container that runs the stock
Jupyter image `quay.io/jupyter/scipy-notebook`, pinned by digest. It has no
network, and it holds no credential except its own token.

## How it works

- The `sandbox` builtin of the gateway gives the agent two tools:
  `run_python(code, files, timeout_s)` and `reset_python()`.
- The gateway calls the Jupyter server in the sandbox container with the
  stock Jupyter REST and kernel API. The container runs `jupyter server`, and
  nothing else of ours.
- Each conversation gets one kernel, in its own folder. A variable or a table
  from one run is there in the next run of the same conversation.
  `reset_python` shuts the kernel down and removes the folder. A request with
  no conversation gets a kernel for one run.
- `files` names files that the person may read. A path is a path of the files
  server (`wiki/...`, `people/...`, `shared/...`) or a document of the person
  or of `shared/` (`documents/<owner>/<slug>.md`). The gateway copies each
  file into `in/` before the run.
- The code writes the files it makes into `out/`. The result gives back each
  file: an image as an image, and a text file (CSV, TSV, TXT, Markdown, JSON,
  HTML, SVG) as text. Another file is named with its size only. `out/` is
  emptied at the start of each run.
- The result holds stdout, stderr, the value of the last line, each image that
  the code displays, and the traceback of an error. Text past
  `sandbox.max_output_chars` is cut, and the result says so.
- A run past its time is interrupted, and the kernel keeps its variables. A
  kernel that does not stop is shut down.

The image holds Python 3, pandas, numpy, scipy, matplotlib, seaborn, altair,
bokeh, openpyxl, xlrd, scikit-learn, statsmodels, and sqlalchemy. The code
cannot install a package, because the container has no network.

A chart for an HTML document is an SVG from matplotlib, put in the HTML. An
HTML document cannot load a script or an image from the network
([documents.md](documents.md)).

## Access

- The sandbox is for members. A guest, and a request with no role, gets no
  tools.
- The person and the role come from the request, never from a tool argument.
- A document path must name the person's own folder or `shared/`. A files path
  goes through the same path rules as the `files` server.
- With `sandbox.enabled: false` the server lists no tools.

## Turn it on with compose

1. Run `make init-env`. It mints `SANDBOX_TOKEN` into `.env`.
2. In `joshua.yaml`, turn the sandbox on and add the server:

   ```yaml
   sandbox:
     enabled: true
   mcp:
     sandbox:
       kind: builtin
       allow: all
   ```

3. Run `make up`. It starts the `sandbox` profile when `sandbox.enabled` is
   true.

The `sandbox` compose network is `internal: true`, and only the gateway and
the sandbox are on it. The container has a read-only root file system, drops
all capabilities, and writes only to two tmpfs mounts, so a restart starts it
empty.

## Turn it on with Kubernetes

1. Make the Secret `joshua-sandbox-secrets` with the key `JUPYTER_TOKEN`. The
   gateway reads the same key as `SANDBOX_TOKEN`.
2. Set `sandbox.enabled: true` in the values of the chart.
3. In `config`, set the sandbox section and add the server:

   ```yaml
   sandbox:
     enabled: true
     url: http://joshua-sandbox:8888
   mcp:
     sandbox:
       kind: builtin
       allow: all
   ```

The chart always gives the pod a NetworkPolicy: ingress from the gateway pod
on port 8888, and no egress, DNS included. A cluster whose network plugin does
not enforce a NetworkPolicy gives the pod a network. Check it before you turn
the sandbox on: from the sandbox pod, a DNS lookup and a request to the
gateway must both fail. The pod gets no service account token, runs as a user
that is not root, has a read-only root file system, and writes only to two
`emptyDir` volumes.

## Settings

| Knob | Default | Effect |
|---|---|---|
| `enabled` | `false` | `true` gives the `sandbox` server its tools. |
| `url` | `http://sandbox:8888` | The Jupyter server, as the gateway reaches it. |
| `run_timeout_s` | `120` | A run stops after this many seconds. A call can ask for less, not more. |
| `max_output_chars` | `20000` | The text of one result. The rest is cut. |
| `max_input_bytes` | `26214400` | The input files of one run together. |
| `max_kernels` | `4` | The kernels that run at the same time. Over the limit, the kernel that was used least recently is shut down. |
| `folder_ttl_hours` | `24` | A folder that no kernel uses is removed after this time. |

The Jupyter server shuts down a kernel that is idle for 30 minutes
(`--MappingKernelManager.cull_idle_timeout`, `sandbox.cullIdleSeconds` in the
chart).

## Known limitation: one container for every kernel

Every kernel runs in the same container, as the same user, on the kernel of
the host. There is no gVisor or other runtime of its own. So:

- The code of one conversation can read the folder of another conversation.
- The code can read the Jupyter token from its environment, and so control the
  other kernels of the sandbox.
- A run that uses all of the memory or the CPU slows the other runs until its
  timeout stops it.

The control that matters is the network. The container reaches nothing, so the
data that the code sees cannot leave it, except as the result that Joshua
reads. It holds no credential of the instance and no file that the gateway did
not copy in. [security.md](security.md#the-code-sandbox) has the rest.
