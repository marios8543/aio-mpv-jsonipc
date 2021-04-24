import logging
logger = logging.getLogger(__name__)
from asyncio import get_event_loop, open_unix_connection, create_subprocess_exec, Queue, Event, sleep, Lock
from asyncio.subprocess import DEVNULL
from inspect import iscoroutine
from os import path, unlink, chmod
import json
from traceback import print_exc

class ResponseEvent:
    def __init__(self):
        self.event = Event()
        self.response = None

    def set_response(self, response):
        self.response = response
        self.event.set()

    async def wait(self):
        await self.event.wait()
        return self.response

class MPVError(Exception):
    """An error originating from MPV or due to a problem with MPV."""
    def __init__(self, *args, **kwargs):
        super(MPVError, self).__init__(*args, **kwargs)

class MPV:
    def __init__(
            self,
            media="",
            socket=None,
            mpv_path="/usr/bin/mpv",
            mpv_args=["--no-audio-display"],
            log_callback=None,
            log_level="error"):
        """
        Create an MPV instance. if you specify a socket, this will not create a new instance and will instead connect to that one.
        If not it will start a new MPV instance according to the mpv_path argument and connect to it. Optionally you can specify a path or URL
        of a media file to play.
        """
        self.properties = set()
        self.event_bindings = {}
        self.property_bindings = {}
        self.key_bindings = {}
        self.unbound_key_callback = None

        self.observer_id = 1
        self.keybind_id = 1
        self.loop = get_event_loop()
        self.media = media
        self.mpv_args = mpv_args
        self.socket = socket
        self.mpv_path = mpv_path
        self.log_callback = log_callback
        self.log_level = log_level
        self.reader, self.writer = None, None
        self.process = None
        self.event_queue = Queue()
        self.wait_queue = None
        self.command_responses = {}
        self.tasks = []
        self.rid = 0
        self.command_lock = Lock()

    def _cleanup(self):
        try:
            unlink("/tmp/mpv-socket.sock")
        except:
            pass

    async def _start_mpv(self):
        self._cleanup()
        self.process = await create_subprocess_exec(
            self.mpv_path,
            "--input-ipc-server=/tmp/mpv-socket.sock",
            self.media,
            *self.mpv_args,
            stdout=DEVNULL,
            stderr=DEVNULL
        )
        self.socket = "/tmp/mpv-socket.sock"

    async def _process_events(self):

        while True:
            data = await self.reader.readline()
            try:
                json_data = json.loads(data)
            except json.decoder.JSONDecodeError:
                break
            logger.debug(json_data)
            if "request_id" in json_data and json_data["request_id"] in self.command_responses:
                self.command_responses[json_data["request_id"]].set_response(json_data)
            else:
                await self.event_queue.put(json_data)
                if self.wait_queue:
                    await self.wait_queue.put(json_data)


    async def _event_dispatcher(self):
        while True:
            data = await self.event_queue.get()
            if data["event"] in self.event_bindings:
                params = { k: v for k, v in data.items() if k != "event"}
                for coro in self.event_bindings[data["event"]]:
                    self.loop.create_task(coro(**params))

    async def _stop(self):
        for task in self.tasks:
            task.cancel()
        self.writer.close()
        await self.writer.wait_closed()
        self._cleanup()

    async def _wait_destroy(self):
        await self.wait_complete()
        await self._stop()

    async def send(self, arguments):
        """
        Coroutine. Sends a command, waits and returns the response.
        """
        async with self.command_lock:
            self.rid += 1
            self.command_responses[self.rid] = ResponseEvent()
            data = json.dumps({
                "command": arguments,
                "request_id": self.rid
            })+"\n"
            data = data.encode("utf-8")
            self.writer.write(data)
            await self.writer.drain()
            response = await self.command_responses[self.rid].wait()
            del self.command_responses[self.rid]
            return response

    async def command(self, *args):
        logger.debug(f"command: {args}")
        result = await self.send(args)
        if result.get("error") != "success":
            raise MPVError("mpv command returned error: %s" %(result.get("error")))

        return result.get("data")

    def listen_for(self, event, func):
        """
        Decorator. This will add a coroutine to be used as a callback for the event specified in the event argument
        """
        if event in self.event_bindings:
            self.event_bindings[event].append(func)
        else:
            self.event_bindings[event] = [func]

    async def get_events(self, event=None):
        """
        Async generator. This will yield events as dictionaries
        """
        self.wait_queue = Queue()
        while True:
            data = await self.wait_queue.get()
            if not event or data["event"] == event:
                yield data
        self.wait_queue = None

    async def start(self):
        """
        Coroutine. Start this MPV instance.
        """
        if not self.socket:
            await self._start_mpv()
        for _ in range(100):
            try:
                self.reader, self.writer = await open_unix_connection(self.socket)
                self.tasks = [
                    self.loop.create_task(self._process_events()),
                    self.loop.create_task(self._event_dispatcher())
                ]
                break
            except FileNotFoundError:
                await sleep(0.1)

        self.properties = set(
            p.replace("-", "_")
            for p in await self.command("get_property", "property-list")
        )

        self.listen_for("property-change", self.on_property_change)
        self.listen_for("client-message", self.on_client_message)

        if self.log_callback is not None and self.log_level is not None:
            await self.command("request_log_messages", self.log_level)
            self.listen_for("log-message", self.on_log_message)

        if self.process:
            self.loop.create_task(self._wait_destroy())

    async def on_log_message(self, level, prefix, text):
        await self.log_callback(level, prefix, text.strip())

    async def on_client_message(self, args):
        if len(args) == 2 and args[0] == "custom-bind":
            self.loop.create_task(self.key_bindings[args[1]]())
        elif (
                self.unbound_key_callback
                and len(args) == 5
                and args[0] == "key-binding"
                and args[1] == "unmapped-keypress"
                and args[2][0] == "d"
        ):
            self.loop.create_task(self.unbound_key_callback(*args[2:]))

    async def on_property_change(self, id, name, data):
        if id in self.property_bindings:
            propname, callback = self.property_bindings[id]
            self.loop.create_task(callback(name, data))


    def bind_property_observer(self, name, callback):
        """
        Bind a callback to an MPV property change.

        *name* is the property name.
        *callback(name, data)* is the function to call.

        Returns a unique observer ID needed to destroy the observer.
        """
        observer_id = self.observer_id
        self.observer_id += 1
        self.property_bindings[observer_id] = name, callback
        self.loop.create_task(self.command("observe_property", observer_id, name))
        return observer_id

    def unbind_property_observer(self, name_or_id):
        if isinstance(name_or_id, int) and name_or_id in self.property_bindings:
            del self.property_bindings[name_or_id]
        elif isinstance(name_or_id, str):
            self.property_bindings = {
                id: (propname, callback)
                for id, (propname, callback) in self.property_bindings.items()
                if propname != name_or_id
            }


    async def bind_key_press(self, name, callback):
        """
        Bind a callback to an MPV keypress event.

        *name* is the key symbol.
        *callback()* is the function to call.
        """
        keybind_id = self.keybind_id
        self.keybind_id += 1

        bind_name = "bind{0}".format(keybind_id)
        self.key_bindings["bind{0}".format(keybind_id)] = callback
        try:
            await self.command("keybind", name, "script-message custom-bind {0}".format(bind_name))
        except MPVError:
            await self.command(
                "define_section", bind_name,
                "{0} script-message custom-bind {1}".format(name, bind_name)
            )
            await self.command("enable_section", bind_name)

    async def register_unbound_key_callback(self, callback):
        self.unbound_key_callback = callback
        await self.command("keybind", "UNMAPPED", "script-binding unmapped-keypress")


    def on_key_press(self, name):
        """
        Decorator to bind a callback to an MPV keypress event.

        @on_key_press(key_name)
        def my_callback():
            pass
        """
        def wrapper(func):
            self.bind_key_press(name, func)
            return func
        return wrapper

    def on_event(self, name):
        """
        Decorator to bind a callback to an MPV event.

        @on_event(name)
        def my_callback(event_data):
            pass
        """
        def wrapper(func):
            self.listen_for(name, func)
            return func
        return wrapper

    async def wait_complete(self):
        """
        Coroutine. Wait for the player to exit. Works when the MPV
        instance is managed by the library.
        """
        await self.process.wait()

    async def stop(self):
        """
        Coroutine. Stop this MPV instance.
        """
        try:
            await self.send(["quit"])
            await self.process.wait()
        except:
            pass

    def __del__(self):
        self.loop.create_task(self.stop())
