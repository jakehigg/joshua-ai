"""The Jupyter client of code execution, against a fake Jupyter server on 127.0.0.1.

The fake speaks the parts of the Jupyter server API that the client uses: the
kernels, the contents, and the kernel websocket with the JSON wire format. It
checks the token on each request. ``test_code_execution_live.py`` runs the same calls
against the real image.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from joshua_gateway.code_execution_mcp.jupyter import CodeExecutionError, Jupyter, strip_ansi

TOKEN = "test-token"


def reply(parent: dict, kind: str, content: dict, channel: str = "iopub") -> str:
    return json.dumps(
        {
            "header": {"msg_type": kind, "msg_id": "r"},
            "parent_header": parent["header"],
            "msg_type": kind,
            "content": content,
            "channel": channel,
        }
    )


class FakeServer:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.kernels: dict[str, str] = {}
        self.interrupts = 0
        app = web.Application(middlewares=[self.auth])
        app.router.add_post("/api/kernels", self.start)
        app.router.add_get("/api/kernels/{kid}", self.kernel)
        app.router.add_delete("/api/kernels/{kid}", self.stop)
        app.router.add_post("/api/kernels/{kid}/interrupt", self.interrupt)
        app.router.add_get("/api/kernels/{kid}/channels", self.channels)
        app.router.add_route("*", "/api/contents/{path:.*}", self.contents)
        self.server = TestServer(app, host="127.0.0.1")

    @web.middleware
    async def auth(self, request, handler):
        if request.headers.get("Authorization") != f"token {TOKEN}":
            return web.json_response({"message": "Forbidden"}, status=403)
        return await handler(request)

    async def start(self, request):
        body = await request.json()
        kid = f"k{len(self.kernels) + 1}"
        self.kernels[kid] = body["path"]
        return web.json_response({"id": kid, "name": body["name"]}, status=201)

    async def kernel(self, request):
        if request.match_info["kid"] not in self.kernels:
            return web.json_response({}, status=404)
        return web.json_response({"id": request.match_info["kid"]})

    async def stop(self, request):
        self.kernels.pop(request.match_info["kid"], None)
        return web.Response(status=204)

    async def interrupt(self, request):
        self.interrupts += 1
        return web.Response(status=204)

    async def channels(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            if frame.type != WSMsgType.TEXT:
                continue
            msg = json.loads(frame.data)
            code = msg["content"]["code"]
            # A message of another request comes first: the client must skip it.
            await ws.send_str(
                reply({"header": {"msg_id": "other"}}, "stream", {"name": "stdout", "text": "no"})
            )
            await ws.send_str(reply(msg, "status", {"execution_state": "busy"}))
            if code == "sleep":
                await asyncio.sleep(5)
            elif code == "fail":
                trace = ["\x1b[0;31mZeroDivisionError\x1b[0m", "division by zero"]
                await ws.send_str(
                    reply(msg, "error", {"ename": "ZeroDivisionError", "traceback": trace})
                )
            else:
                await ws.send_str(reply(msg, "stream", {"name": "stdout", "text": "138\n"}))
                await ws.send_str(reply(msg, "stream", {"name": "stderr", "text": "warn\n"}))
                await ws.send_str(reply(msg, "execute_result", {"data": {"text/plain": "3"}}))
                await ws.send_str(
                    reply(
                        msg,
                        "display_data",
                        {"data": {"image/png": "iVBO\nRw==", "text/plain": "<Figure>"}},
                    )
                )
            await ws.send_str(reply(msg, "status", {"execution_state": "idle"}))
        return ws

    async def contents(self, request):
        path = request.match_info["path"]
        if request.method == "PUT":
            body = await request.json()
            if body["type"] == "directory":
                self.files.setdefault(path + "/", "")
            else:
                self.files[path] = body["content"]
            return web.json_response({"path": path}, status=201)
        if request.method == "DELETE":
            if path + "/" in self.files:
                if any(k.startswith(path + "/") and k != path + "/" for k in self.files):
                    return web.json_response({"message": "not empty"}, status=400)
                del self.files[path + "/"]
            elif self.files.pop(path, None) is None:
                return web.json_response({}, status=404)
            return web.Response(status=204)
        if path + "/" in self.files:
            names = {
                k[len(path) + 1 :].split("/")[0] + ("/" if "/" in k[len(path) + 1 :] else "")
                for k in self.files
                if k.startswith(path + "/") and k != path + "/"
            }
            content = [
                {
                    "name": n.rstrip("/"),
                    "path": f"{path}/{n.rstrip('/')}",
                    "type": "directory" if n.endswith("/") else "file",
                    "size": None
                    if n.endswith("/")
                    else len(base64.b64decode(self.files[f"{path}/{n}"])),
                    "last_modified": "2026-11-24T10:00:00Z",
                }
                for n in sorted(names)
            ]
            return web.json_response({"type": "directory", "content": content})
        if path in self.files:
            return web.json_response(
                {"type": "file", "format": "base64", "content": self.files[path]}
            )
        return web.json_response({}, status=404)


@pytest.fixture
async def fake():
    server = FakeServer()
    await server.server.start_server()
    yield server
    await server.server.close()


@pytest.fixture
async def client(fake):
    jupyter = Jupyter(str(fake.server.make_url("")).rstrip("/"), TOKEN)
    yield jupyter
    await jupyter.close()


async def test_a_run_collects_its_own_messages_until_idle(fake, client):
    kid = await client.start_kernel("conversations/c-1")
    run = await client.execute(kid, "go", timeout_s=5)
    assert fake.kernels[kid] == "conversations/c-1"
    assert run.stdout == "138\n"
    assert run.stderr == "warn\n"
    assert run.results == ["3"]
    assert run.images == [("image/png", "iVBORw==")]
    assert run.error is None and run.timed_out is False


async def test_an_error_gives_the_traceback_without_colors(client):
    kid = await client.start_kernel("c")
    run = await client.execute(kid, "fail", timeout_s=5)
    assert run.error == "ZeroDivisionError\ndivision by zero"


async def test_a_slow_run_times_out(fake, client):
    kid = await client.start_kernel("c")
    run = await client.execute(kid, "sleep", timeout_s=0.3)
    assert run.timed_out is True
    await client.interrupt(kid)
    assert fake.interrupts == 1


async def test_the_kernel_calls(client):
    kid = await client.start_kernel("c")
    assert await client.kernel_alive(kid) is True
    await client.shutdown(kid)
    assert await client.kernel_alive(kid) is False
    await client.shutdown(kid)  # a second shutdown is not an error


async def test_files_go_in_come_out_and_a_tree_is_removed(client):
    await client.mkdir("c")
    await client.mkdir("c/out")
    await client.put_file("c/out/rows.csv", b"a,b\n1,2\n")
    await client.put_file("c/in.txt", b"x")
    names = sorted((e.name, e.type) for e in await client.listdir("c"))
    assert names == [("in.txt", "file"), ("out", "directory")]
    (entry,) = await client.listdir("c/out")
    assert entry.size == 8 and entry.last_modified is not None
    assert await client.get_file("c/out/rows.csv") == b"a,b\n1,2\n"
    await client.remove_tree("c")
    assert await client.listdir("c") == []


async def test_a_wrong_token_is_a_refusal_that_names_no_secret(fake):
    jupyter = Jupyter(str(fake.server.make_url("")).rstrip("/"), "wrong")
    with pytest.raises(CodeExecutionError) as exc:
        await jupyter.start_kernel("c")
    await jupyter.close()
    assert exc.value.message == "the code execution container refused to start a kernel (403)"


async def test_no_server_is_a_plain_error():
    jupyter = Jupyter("http://127.0.0.1:9", TOKEN)
    with pytest.raises(CodeExecutionError, match="did not answer"):
        await jupyter.start_kernel("c")
    await jupyter.close()


def test_strip_ansi():
    assert strip_ansi("\x1b[0;31mError\x1b[0m: x") == "Error: x"
