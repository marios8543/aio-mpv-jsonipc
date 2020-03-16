from asyncio import get_event_loop, open_unix_connection, create_subprocess_exec, Queue, Event, sleep, Lock
from asyncio.subprocess import DEVNULL
from inspect import iscoroutine
from os import path, unlink, chmod
from json import dumps, loads
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

class MPV:
    def __init__(self, media="", socket=None, mpv_path="/usr/bin/mpv", mpv_args=["--no-audio-display"]):
        """
        Create an MPV instance. if you specify a socket, this will not create a new instance and will instead connect to that one.
        If not it will start a new MPV instance according to the mpv_path argument and connect to it. Optionally you can specify a path or URL
        of a media file to play.
        """
        self.loop = get_event_loop()
        self.media = media
        self.mpv_args = mpv_args
        self.socket = socket
        self.mpv_path = mpv_path
        self.reader, self.writer = None, None
        self.process = None
        self.callback_queue = Queue()
        self.wait_queue = None
        self.command_responses = {}
        self.callbacks = {}
        self.tasks = []
        self.rid = 0
        self.socket_lock = Lock()

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
            try:
                data = await self.reader.readline()
                data = loads(data.decode("utf-8"))
                if "request_id" in data and data["request_id"] in self.command_responses:
                    self.command_responses[data["request_id"]].set_response(data)
                else:
                    await self.callback_queue.put(data)
                    if self.wait_queue:
                        await self.wait_queue.put(data)
            except Exception:
                print_exc()
            finally:
                await sleep(0.1)

    async def _callback_dispatcher(self):
        while True:
            data = await self.callback_queue.get()
            if data["event"] in self.callbacks:
                for coro in self.callbacks[data["event"]]:
                    self.loop.create_task(coro(data["data"]))

    async def send(self, arguments):
        """
        Coroutine. Sends a command, waits and returns the response.
        """
        self.rid += 1
        self.command_responses[self.rid] = ResponseEvent()
        data = dumps({
            "command": arguments,
            "request_id": self.rid
        })+"\n"
        data = data.encode("utf-8")
        async with self.socket_lock:
            self.writer.write(data)
            await self.writer.drain()
        response = await self.command_responses[self.rid].wait()
        del self.command_responses[self.rid]
        return response

    def listen_for(self, event, func):
        """
        Decorator. This will add a coroutine to be used as a callback for the event specified in the event argument
        """
        if event in self.callbacks:
            self.callbacks[event].append(func)
        else:
            self.callbacks[event] = [func]

    async def get_events(self, event=None):
        """
        Async generator. This will yield events as dictionaries
        """
        self.wait_queue = Queue()
        while True:
            data = await self.wait_queue.get()
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
                    self.loop.create_task(self._callback_dispatcher())
                ]
                break
            except FileNotFoundError:
                await sleep(0.1)

    async def wait_complete(self):
        await self.process.wait()

    async def stop(self):
        """
        Coroutine. Stop this MPV instance.
        """
        self.process.terminate()
        self.writer.close()
        await self.writer.wait_closed()
        for task in self.tasks:
            task.cancel()

    def __del__(self):
        self.loop.create_task(self.stop())