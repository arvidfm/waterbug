#   Waterbug, a modular IRC bot written using Python 3
#   Copyright (C) 2011  Arvid Fahlström Myrman
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import collections
import functools
import importlib
import inspect
import logging
import pkgutil
import shelve
import sys
import traceback
import types

import waterbug.modules
import waterbug.network

BANNED = 0
STANDARD = 1
TRUSTED = 2
ELEVATED = 3
OP = 4
ADMIN = 5

class Waterbug:

    def __init__(self, prefix='%', *, loop=None):
        self.servers = {}
        self.commands = {}
        self.modules = []
        self.prefix = prefix

        self.data = shelve.open("data.pck")

        self.config = {
            "waterbug": {
                "prefix": "%"
            },
            "servers": {
                "FreeNode": {
                    "hostname": "chat.freenode.net",
                    "port": 6667,
                    "autojoin": ["##FireFly"],
                    "privileges": {
                        "unaffiliated/beholdmyglory": ADMIN
                    }
                },
                #"FreeQuest": {
                    #"hostname": "irc.freequest.net",
                    #"port": 6667,
                    #"autojoin": ["#datorklubben"],
                    #"privileges": {
                        #"unaffiliated/beholdmyglory": ADMIN
                    #}
                #}
            },
            "modules": {
                "anidb": {
                    "server": "api.anidb.net",
                    "port": 9001,
                    "protoversion": 1,
                    "clientname": "eldishttp",
                    "clientversion": 1
                }
            }
        }

        self.privileges = {
            "unaffiliated/beholdmyglory": ADMIN
        }

        self.loop = loop or asyncio.get_event_loop()
        self._future = None

    @asyncio.coroutine
    def run(self):
        self._future = asyncio.Future()
        self.load_modules()
        yield from self.open_connections()
        yield from self._future
        self._future = None

    @asyncio.coroutine
    def open_connections(self):
        def _open_connection(server):
            def connection_closed(future):
                if server.reconnect:
                    logging.info("Reconnecting to %s", server.connection_name)
                    _open_connection(server)
                else:
                    logging.info("Removing %s from server list", server.connection_name)
                    del self.servers[server.connection_name]

            asyncio.async(server.connect(), loop=self.loop).add_done_callback(connection_closed)

        for name, config in self.config['servers'].items():
            server = waterbug.network.Server(config['hostname'], config['port'],
                                             name, self, autojoin=config['autojoin'],
                                             loop=self.loop)
            self.servers[name] = server

            _open_connection(server)

    def quit(self):
        for server in self.servers.values():
            server.quit()
        self.unload_modules()
        self._future.set_result(None)

    def unload_modules(self):
        for module in self.modules:
            if hasattr(module.commands, "unload"):
                if getattr(module.commands.unload, "trigger", False):
                    module.commands.unload()
        self.modules = []
        self.commands = {}

    def load_modules(self):
        self.unload_modules()

        for _, module_name, _ in pkgutil.iter_modules(waterbug.modules.__path__):
            try:
                logging.info("Loading %s", module_name)
                module = types.ModuleType(module_name)
                with open('waterbug/modules/{}.py'.format(module_name)) as f:
                    code = compile(f.read(), module_name, 'exec')
                    exec(code, module.__dict__, module.__dict__)
                self.modules.append(module)
            except Exception:
                traceback.print_exc()

        for module in self.modules:
            try:

                module_data = Waterbug.ModuleStorage(module.__name__, self.data)

                def add_commands(cobj, clist):
                    cobj.data = module_data
                    if hasattr(cobj, "init") and getattr(cobj.init, "trigger", False):
                        cobj.init()
                    for name, value in inspect.getmembers(cobj):
                        if getattr(value, "exposed", False):
                            if callable(value):
                                clist[value.__name__] = value
                            else:
                                clist[name] = {}
                                add_commands(value, clist[name])


                module.commands = module.Commands()
                add_commands(module.commands, self.commands)

            except BaseException:
                traceback.print_exc()

    class ModuleStorage:

        def __init__(self, name, data):
            self.name = name
            if name not in data:
                data[name] = {}
            self.data = data[name]
            self._data = data

        def sync(self):
            self._data[self.name] = self.data
            self._data.sync()

        def get_data(self):
            return self.data

    class Responder:

        def __init__(self, bot, server, sender, target, receiver, line):
            self.bot = bot
            self.server = server
            self.sender = sender
            self.target = target
            self.receiver = receiver
            self.line = line

        def __call__(self, msg, target=None, msgtype='PRIVMSG'):
            if msgtype == 'PRIVMSG':
                target = target or self.target
                self.server.msg(target, msg)
            elif msgtype == 'NOTICE':
                target = target or self.sender.username
                self.server.notice(target, msg)


    def get_command(self, args):
        commands = self.commands
        command_length = 0

        while (command_length < len(args)
                and isinstance(commands, collections.Mapping)
                and args[command_length] in commands):
            commands = commands[args[command_length]]
            command_length += 1

        if callable(commands):
            func = commands
        elif (isinstance(commands, collections.Mapping)
                and "_default" in commands
                and callable(commands["_default"])):
            func = commands["_default"]
        else:
            raise LookupError("Couldn't find a matching command")

        return func, args[command_length:]

    def on_privmsg(self, server, sender, receiver, message):
        if receiver[0] in server.supported['CHANTYPES']:
            target = receiver
        else:
            target = sender
        if message.startswith(self.prefix):
            message = message[1:]

            try:
                func, args = self.get_command(message.split(" "))
            except LookupError:
                return

            if sender.access >= func.access:
                responder = Waterbug.Responder(self, server, sender, target,
                                               receiver, " ".join(args))
                try:
                    func(responder, *args)
                except TypeError:
                    responder("Wrong number of arguments")
                except BaseException:
                    traceback.print_exc()
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    exc = traceback.format_exception_only(exc_type, exc_value)
                    stack = traceback.format_tb(exc_traceback)
                    exception = "{}: {}".format(stack[-1], "".join(exc)).replace("\n", "")
                    server.msg(target, exception)
            else:
                server.msg(target, "You do not have access to this command")

def expose(name=None, access=STANDARD):
    def decorator(target):
        target.exposed = True
        target.access = access
        if name is not None:
            target.__name__ = name
        if target.__doc__ is None:
            target.__doc__ = "No help available for this command"
        return target
    return decorator

def trigger(target):
    target.trigger = True
    return target

