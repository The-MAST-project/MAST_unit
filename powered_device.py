
from enum import Enum
import time
from typing import TypeAlias
from utils import return_with_status

PowerType: TypeAlias = "PoweredDevice"


class PowerState(Enum):
    Off = 0
    On = 1
    Unknown = 2
    AllOn = 3
    AllOff = 4


class SocketId:
    id: int
    name: str

    def __init__(self, _id: int | str):
        if isinstance(_id, str):
            self.name = _id


class Socket:
    id: SocketId
    state: PowerState
    dev: None

    def __init__(self, _id: SocketId, state: PowerState):
        self.id = _id
        self.state = state

    @staticmethod
    def names() -> list[str]:
        names = []
        for sock in sockets:
            names.append(sock.id.name)
        return names


sockets: list[Socket] = [
    Socket(SocketId('Mount'), state=PowerState.Off),
    Socket(SocketId('Camera'), state=PowerState.Off),
    Socket(SocketId('Stage'), state=PowerState.Off),
    Socket(SocketId('Covers'), state=PowerState.Off),
    Socket(SocketId('Focuser'), state=PowerState.Off),
]
for i, s in enumerate(sockets):
    s.id.id = i


class SocketStatus:
    name: str
    state: PowerState
    state_verbal: str

    def __init__(self, name: str, state: PowerState):
        self.name = name
        self.state = state
        self.state_verbal = state.name


class PowerStatus:
    sockets: list
    is_operational: bool = True
    reasons: list[str]

    def __init__(self):
        self.sockets = []
        self.reasons = list()
        for index, socket in enumerate(sockets):
            self.sockets.append(SocketStatus(name=socket.id.name, state=socket.state))
            if socket.state != PowerState.On:
                self.is_operational = False
                self.reasons.append(f'socket[{socket.id.name}] is OFF')


class PoweredDevice:

    socket: Socket
    dev: None

    def __init__(self, socket_name: str, dev):
        self.dev = dev
        for sock in sockets:
            if sock.id.name == socket_name:
                self.socket = sock
                self.socket.dev = dev
                return
        raise f"No socket named '{socket_name}'"

    @return_with_status
    def power(self, wanted_state: PowerState | str):
        if isinstance(wanted_state, str):
            wanted_state = PowerState(wanted_state)

        self.socket.state = wanted_state
        self.dev.logger.info(f"Turned socket[{self.socket.id.name}] to '{wanted_state}'")
        time.sleep(2)

    def power_on(self):
        self.power(PowerState.On)

    def power_off(self):
        self.power(PowerState.Off)

    def power_state(self) -> PowerState:
        return self.socket.state

    def startup(self):
        pass

    def shutdown(self):
        pass

    @staticmethod
    def status():
        return PowerStatus()

    def is_powered(self) -> bool:
        return self.socket.state == PowerState.On

    @staticmethod
    def all_on():
        for sock in sockets:
            dev = PoweredDevice(sock.id.name, sock.dev)
            dev.power_on()

    @staticmethod
    def all_off():
        for sock in sockets:
            sock.state = PowerState.Off


def name2id(_id: SocketId) -> int:
    for index, socket in enumerate(sockets):
        if _id.name == socket.id.name:
            return socket.id.id
    return -1


def id2name(_id: int) -> str:
    for index, socket in enumerate(sockets):
        if _id == index:
            return socket.id.name
    return ''


