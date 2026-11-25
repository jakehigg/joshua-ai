# Code execution

Code execution lets Joshua run Python. Joshua uses it to count, filter, sort,
compare, and chart data, so a number that comes from a file comes from code,
not from a read of the file. Example: two exports of one reading list, and the
question "which book is in one list and not in the other?".

Code execution is off by default. The code runs in a separate container that
runs the stock Jupyter image `quay.io/jupyter/scipy-notebook`, pinned by
digest. That container has no network, and it holds no credential except its
own token.

## The parts

```
 core (agent session)
   │  MCP tool call: run_python / reset_python
   ▼
 gateway ── code-execution builtin (gateway/joshua_gateway/code_execution_mcp/)
   │  Jupyter REST API (/api/kernels, /api/contents)
   │  kernel websocket (/api/kernels/<id>/channels)
   │  Authorization: token $CODE_EXECUTION_TOKEN
   ▼
 code execution container ── stock `jupyter server`, one kernel per conversation
   (no route out: no DNS, no internet, no cluster, no other container)
```

- **The builtin.** `code-execution` is a builtin MCP server of the gateway,
  like `files` and `documents`. It runs in the gateway process and gives the
  agent two tools: `run_python` and `reset_python`.
  - `server.py` holds the tools, the kernels of the conversations, and the
    access rules.
  - `jupyter.py` is the small client of the Jupyter API. It uses `aiohttp`.
- **The container.** It runs `jupyter server` from the stock image and nothing
  else of ours. There is no API of our own in it. The gateway uses the
  standard Jupyter REST API and the kernel wire protocol.
- **The skill.** A member session that holds the server gets the skill page
  `core/joshua_core/prompts/builtin/skills/code-execution.md`. It tells the
  agent to compute and not to count in its head, and how to use `in/` and
  `out/`.

## What happens in one run

1. The agent calls `run_python(code, files, timeout_s)`.
2. The gateway checks the request:
   - code execution is on;
   - the caller is a member;
   - the code is at most 100,000 characters;
   - there are at most 20 files.
3. The gateway reads each file in `files`. It uses the person from the
   request, never a tool argument. The files of one run together must not be
   larger than `max_input_bytes`.
4. The gateway finds the kernel of the conversation, or starts one in the
   folder of the conversation (see [Kernels](#kernels)).
5. The gateway empties `out/`, and copies each input file into `in/`. When
   two files have the same name, the second one gets a number (`list-1.csv`).
6. The gateway sends the code to the kernel as an `execute_request` on the
   websocket. It collects stdout, stderr, the value of the last line, each
   image that the code displays (PNG or JPEG), and the traceback of an error.
   It stops when the kernel reports that it is idle.
7. The gateway reads `out/` and gives back each file:
   - an image (PNG, JPEG, GIF, WebP) up to 4 MB comes back as an image;
   - a text file (CSV, TSV, TXT, Markdown, JSON, HTML, SVG, XML, PY, LOG)
     comes back as text;
   - any other file is named with its size only.

   At most 10 files and 6 images come back from one run.
8. Text past `max_output_chars` is cut, and the result says how much was cut.

A run that goes past its time is interrupted, and the kernel keeps its
variables. If the kernel is not idle 5 seconds after the interrupt, the
gateway shuts it down. The result then says that the state is gone.

The code cannot write a file back into the wiki or the documents. To keep a
result, the agent reads it from the result and writes it with the `files` or
`documents` tools. Example: an HTML report with a chart is saved as a
document.

## Files and folders

The root of the Jupyter server is `/home/jovyan`, which is a tmpfs (compose)
or an `emptyDir` (Kubernetes). In it:

```
conversations/
  c-<sha256 of the conversation id, 24 hex characters>/
    in/     the input files of the last run
    out/    the files that the code makes; emptied at the start of each run
    ...     anything else that the code writes stays until the folder is removed
```

The folder name is a hash, so it names no person and no conversation. The
working directory of the kernel is the folder, so the code reads
`in/list.csv` and writes `out/chart.svg`.

A request with no conversation gets a kernel and a folder for that one run.
Both are removed after the run.

## Kernels

- **One kernel for each conversation.** The first run of a conversation
  starts a kernel. The next runs use the same kernel, so a variable or a table
  is still there.
- **A cap.** At most `max_kernels` kernels run at the same time. To start one
  more, the gateway shuts down the kernel that was used least recently. Its
  folder stays, so a later run starts a new kernel with the old files.
- **Idle kernels.** The Jupyter server shuts down a kernel that is idle for
  30 minutes (`--MappingKernelManager.cull_idle_timeout`, `codeExecution.cullIdleSeconds`
  in the chart). The next run of that conversation starts a new kernel.
- **Old folders.** At most every 10 minutes, a run removes the conversation
  folders that no live kernel uses and that did not change for
  `folder_ttl_hours`.
- **`reset_python`.** It shuts down the kernel of the conversation and
  removes its folder.
- **A restart of the container.** It loses every kernel and every file. The
  gateway sees that the kernel is gone and starts a new one at the next run.
  The agent must then load its data again.
- **One run at a time.** Two runs of one conversation wait for each other.
  Runs of different conversations run at the same time.

The gateway keeps the map from conversation to kernel in memory. A restart of
the gateway forgets it, and the next run starts a new kernel. The old kernel
is shut down by the idle cull. That is also why the chart runs one replica,
with the `Recreate` strategy.

## What is installed

Python 3, pandas, numpy, scipy, matplotlib, seaborn, altair, bokeh, openpyxl,
xlrd, scikit-learn, statsmodels, and sqlalchemy. The code cannot install a
package, because the container has no network. To get a new package, pin a
newer digest of the image that has it.

A chart for an HTML document is an SVG from matplotlib, put in the HTML. An
HTML document cannot load a script or an image from the network
([documents.md](documents.md)). A chart for the chat is a PNG.

## Access

- Code execution is for members. A guest, and a request with no role, gets
  no tools. A direct call from a guest is refused.
- The person, the role, and the conversation come from the request, never from
  a tool argument.
- A files path (`wiki/...`, `people/...`, `shared/...`) goes through the same
  path rules as the `files` server.
- A document path (`documents/<owner>/<slug>.md` or `.html`) must name the
  person's own folder or `shared/`.
- With `code_execution.enabled: false` the server lists no tools.

## Turn it on with compose

1. Run `make init-env`. It mints `CODE_EXECUTION_TOKEN` into `.env`.
2. In `joshua.yaml`, turn code execution on and add the server:

   ```yaml
   code_execution:
     enabled: true
   mcp:
     code-execution:
       kind: builtin
       allow: all
   ```

3. Run `make up`. It starts the `code-execution` profile when
   `code_execution.enabled` is true.
4. Add `code-execution` to the servers of each workspace that needs it.

The `code-execution` compose network is `internal: true`, and only the gateway
and the code execution container are on it. The container has a read-only
root file system, drops all capabilities, has `no-new-privileges`, and writes
only to two tmpfs mounts, so a restart starts it empty. It refuses to start
with an empty token, because Jupyter turns its auth off with an empty token.

## Turn it on with Kubernetes

1. Make the Secret `joshua-code-execution-secrets` with the key
   `JUPYTER_TOKEN`. The gateway reads the same key as `CODE_EXECUTION_TOKEN`.
   Use `secrets.codeExecution.name` for another Secret name.
2. Set `codeExecution.enabled: true` in the values of the chart.
3. In `config`, set the `code_execution` section and add the server:

   ```yaml
   code_execution:
     enabled: true
     url: http://joshua-code-execution:8888
   mcp:
     code-execution:
       kind: builtin
       allow: all
   ```

The chart always gives the pod a NetworkPolicy: ingress from the gateway pod
on port 8888, and no egress, DNS included. A cluster whose network plugin does
not enforce a NetworkPolicy gives the pod a network. Check the policy before
you turn code execution on (see [Check it](#check-it)). The pod gets no
service account token and no service links. It runs as a user that is not
root, has a read-only root file system, drops all capabilities, and writes
only to two `emptyDir` volumes.

## Settings

| Knob | Default | Effect |
|---|---|---|
| `enabled` | `false` | `true` gives the `code-execution` server its tools. |
| `url` | `http://code-execution:8888` | The Jupyter server, as the gateway reaches it. |
| `run_timeout_s` | `120` | A run stops after this many seconds (5 to 900). A call can ask for less, not more. |
| `max_output_chars` | `20000` | The text of one result. The rest is cut. |
| `max_input_bytes` | `26214400` | The input files of one run together. |
| `max_kernels` | `4` | The kernels that run at the same time (1 to 32). |
| `folder_ttl_hours` | `24` | A folder that no kernel uses is removed after this time. |

The chart values are `codeExecution.image`, `codeExecution.cullIdleSeconds`
(1800), `codeExecution.workSizeLimit` (the size of the home `emptyDir`, 2Gi),
and `codeExecution.resources`. Compose sets 4 GB of memory, 2 CPUs, and 256
processes.

## Check it

From the code execution pod, every request must fail:

```sh
kubectl -n <ns> exec deploy/joshua-code-execution -- python -c \
  "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=5)"
kubectl -n <ns> exec deploy/joshua-code-execution -- python -c \
  "import socket; socket.gethostbyname('kubernetes.default')"
```

From the gateway, a request with the token must get 200, and a request
without it must get 403:

```sh
kubectl -n <ns> exec deploy/joshua-gateway -- python -c "
import os, urllib.request as u
r = u.Request('http://joshua-code-execution:8888/api/kernels',
              headers={'Authorization': 'token ' + os.environ['CODE_EXECUTION_TOKEN']})
print(u.urlopen(r).status)"
```

From core, a request to the pod must fail. The gateway log has one
`code execution run` line for each run, with the number of files, the number
of outputs, whether there was an error or a timeout, and the time. The log
does not hold the code or the data.

## Troubleshooting

| The agent sees | The cause |
|---|---|
| No `run_python` tool | `code_execution.enabled` is false, the person is a guest, or the workspace does not hold `code-execution`. |
| `code execution has no token` | `CODE_EXECUTION_TOKEN` is empty in the gateway. |
| `the code execution container did not answer` | The container is down, or `url` is wrong. |
| `the code execution container refused ... (403)` | The token of the gateway and the token of the container are different. |
| `that folder is not yours` | A document path names the folder of another person. |
| The state is gone | The kernel was culled, the kernel was dropped for the cap, the run timed out, or the container or the gateway restarted. Load the data again. |

## Known limitation: one container for every kernel

Every kernel runs in the same container, as the same user, on the kernel of
the host. There is no gVisor or other runtime of its own. So:

- The code of one conversation can read the folder of another conversation.
- The code can read the Jupyter token from its environment, and so control the
  other kernels.
- A run that uses all of the memory or the CPU slows the other runs until its
  timeout stops it.

The control that matters is the network. The container reaches nothing, so the
data that the code sees cannot leave it, except as the result that Joshua
reads. It holds no credential of the instance and no file that the gateway did
not copy in. [security.md](security.md#code-execution) has the rest.

## Tests

- `gateway/tests/test_code_execution_mcp.py`: the tools, the kernels, the files, and the
  access, against a fake Jupyter client.
- `gateway/tests/test_code_execution_jupyter.py`: the Jupyter client, against a fake
  Jupyter server.
- `gateway/tests/test_code_execution_live.py`: the real image (the `code-execution`
  job of CI, `-m integration`).
- `tests/test_code_execution_isolation.py`: the compose service and network, and the
  chart's Deployment and NetworkPolicy.
- `shared/tests/test_config_code_execution.py`: the config section.
