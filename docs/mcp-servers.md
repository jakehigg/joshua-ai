# Add an MCP server

An MCP server gives Joshua a new capability, such as the weather or control of
your house. Most MCP servers ship as a package. Name the package in your config
file and the gateway installs it at the next start. You do not build an image.

You choose the servers. Joshua cannot add one, and cannot read this config
file. See [docs/security.md](security.md#package-mcp-servers) for what you
accept when you add a server.

The catalog below has a tested entry for the weather and for Home Assistant.
Start there. [docs/config.md](config.md#servers-the-gateway-installs) is the
reference for every field, and for the other two package kinds.

## Add a server on Docker Compose

1. Copy the entry for the server you want from the catalog below.
2. Open `joshua.yaml`. Add the entry under `mcp:`.
3. If the entry names a `${VAR:-}` credential, open `.env.gateway`.
4. Add one `NAME=value` line for each credential. Make the file if it is absent.
5. Run `make validate`. Correct each error that it names.
6. Run `docker compose restart gateway`.
7. Check the result. Read "Check the result" below.

The first start after step 6 is slow, because the gateway installs the package.
Every later start reads the store and is fast.

## Add a server on Kubernetes

1. Copy the entry for the server you want from the catalog below.
2. Open your values file. Add the entry under `mcp:` in the `config:` block.
3. If the entry names a `${VAR:-}` credential, add the value to your Secret.
4. Add the same key name to `secrets.gateway.keys`.
5. Run `helm upgrade`.
6. Check the result. Read "Check the result" below.

Helm restarts the gateway pod, because the config change moves the
`checksum/config` annotation. The store volume keeps the servers you installed
before, so the pod installs only the new one.

## Check the result

This command says which servers the store holds:

```
docker compose exec gateway python -m joshua_gateway mcp check
```

A server that is ready shows `installed` and the version. A server that is not
ready shows `not installed` or `changed`.

This command reads the gateway state:

```
docker compose exec core python -c "import urllib.request; print(urllib.request.urlopen('http://gateway:8000/readyz').read().decode())"
```

| Field | Meaning |
|---|---|
| `connected` | the servers that answer now |
| `installing` | the installs that still run. Wait, then read it again |
| `disabled` | the entries with an empty credential |
| `errored` | the entries that failed to install or failed to connect |

`GET /admin/inventory` names each server, its version, and the tools it gives
to a person. [docs/contracts.md](contracts.md) has every admin route.

## If a server does not start

| What you see | What to do |
|---|---|
| The config load stops and names a pin format | The version is not exact. Write `npm:<name>@1.2.3`, not `@latest` and not a range. |
| `/readyz` counts one in `disabled` | A credential is empty. Run `docker compose logs gateway`. The log names the key. Add the value, then restart the gateway. |
| `/readyz` counts one in `errored` | Run `docker compose logs gateway`. The log holds one line with the reason. |
| The log says `set 'command'` | The install holds more than one program. Add `command:` to the entry with the name of the one to start. |
| The log says `npm is not in the gateway image` | Your gateway image is older than this feature. Update to a release that has it. [docs/CHANGELOG.md](CHANGELOG.md) names the release. |

To install one server again, without a restart:

```
docker compose exec gateway python -m joshua_gateway mcp install <name> --reinstall
```

## Raise a version

1. Change the version in the `package:` field.
2. Run `docker compose restart gateway`, or run `helm upgrade`.

The gateway installs the new version and starts it. The other servers keep
their sessions. If the new version does not install, the gateway keeps the
version you run now.

## The catalog

Every entry below pins a tested version. Read it as a starting point, not as
the newest release. Read the release notes of the package before you raise it.

No token belongs in `joshua.yaml`. Each entry names its credentials as
`${VAR:-}`, and you put the values in `.env.gateway` or in `secrets.gateway`.

### Weather

NOAA and Open-Meteo. It needs no account and no key.

```yaml
mcp:
  weather:
    type: stdio
    package: npm:@dangahagan/weather-mcp@1.6.1
    env:
      ENABLED_TOOLS: standard
    allow: all
```

`ENABLED_TOOLS: standard` selects the server's own preset: `get_forecast`,
`get_current_conditions`, `get_alerts`, `get_historical_weather`,
`search_location`, and `check_service_status`.

### Home Assistant

Read the house, control a device, and read a to-do list.

```yaml
mcp:
  ha:
    type: stdio
    package: pypi:ha-mcp==7.8.1
    env:
      HOMEASSISTANT_URL: https://home-assistant.example.net
      HOMEASSISTANT_TOKEN: ${HOMEASSISTANT_TOKEN:-}
    allow: all
    tools:
      allow:
        - ha_get_overview
        - ha_search
        - ha_get_state
        - ha_get_entity
        - ha_get_device
        - ha_list_floors_areas
        - ha_list_services
        - ha_call_service
        - ha_bulk_control
        - ha_get_history
        - ha_eval_template
        - ha_config_get_automation
        - ha_config_get_scene
        - ha_config_get_script
        - ha_get_todo
        - ha_set_todo_item
        - ha_get_system_health
```

The server has 77 tools. The list above is the read path plus device control
and to-dos. It leaves out every tool that writes a configuration
(`ha_config_set_*`), restarts Home Assistant, or manages a backup, an
integration, or a blueprint. With those out, the agent cannot make an
automation or restart the house.

Make the token in Home Assistant: your profile, **Security**, **Long-lived
access tokens**.

## What this mechanism does not run

**Playwright, and anything that needs a browser.** `@playwright/mcp` starts a
browser, and the gateway image has none. Run it as its own container with a
browser in it, and reach it with `type: http`.

**A server that ships only as a container image.** That is an addon or a
`type: http` entry. See [docs/config.md](config.md#mcp-servers).
