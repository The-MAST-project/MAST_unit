
from enum import Enum
import logging
import time
from typing import TypeAlias
from utils import return_with_status

PowerType: TypeAlias = "Power"

logger = logging.getLogger('mast.unit.power')


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

ip_address: str = ''


class Power:

    @staticmethod
    @return_with_status
    def power(which_socket: SocketId, wanted_state: PowerState | str):
        if isinstance(wanted_state, str):
            wanted_state = PowerState(wanted_state)

        which_socket.id = name2id(which_socket)
        validate(which_socket)
        sockets[which_socket.id].state = wanted_state
        logger.info(f'Turned socket[{sockets[which_socket.id].id.name}] to {wanted_state}')
        time.sleep(2)

    @staticmethod
    def state(which_socket: SocketId) -> PowerState:
        sock = None
        for sock in sockets:
            if sock.id.name == which_socket.name:
                break
        return sock.state if sock else None

    # @return_with_status
    @staticmethod
    def startup():
        pass

    # @return_with_status
    @staticmethod
    def shutdown():
        pass

    @staticmethod
    def status():
        return PowerStatus()

    @staticmethod
    def is_on(which_socket: SocketId) -> bool:
        for sock in sockets:
            if sock.id.name == which_socket.name:
                return sock.state == PowerState.On
        return False

    @staticmethod
    def all_on():
        for sock in sockets:
            Power.power(SocketId(sock.id.name), PowerState.On)

    @staticmethod
    def all_off():
        for s in sockets:
            Power.power(SocketId(s.id.name), PowerState.Off)


def validate(socket_id: SocketId):
    if socket_id.name not in [sock.id.name for sock in sockets]:
        raise f'Invalid socket_id={socket_id}.  Must be [0..{len(sockets)}]'


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


