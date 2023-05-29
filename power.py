
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


class Socket:
    name: str
    state: PowerState

    def __init__(self, name: str, state: PowerState):
        self.name = name
        self.state = state


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

    def __init__(self):
        self.sockets = []
        for index, socket in enumerate(sockets):
            self.sockets.append(SocketStatus(name=socket.name, state=socket.state))
            if socket.state != PowerState.On:
                self.is_operational = False


sockets: list[Socket] = [
    Socket(name='Mount', state=PowerState.Off),
    Socket(name='Camera', state=PowerState.Off),
    Socket(name='Stage', state=PowerState.Off),
    Socket(name='Covers', state=PowerState.Off)
    ]
ip_address: str = ''


class Power:

    @return_with_status
    @staticmethod
    def power(which_socket: int | str, wanted_state: PowerState):
        if isinstance(which_socket, str):
            which_socket = name2id(which_socket)
        validate(which_socket)
        sockets[which_socket].state = wanted_state

    @staticmethod
    def state(which_socket: int | str) -> PowerState:
        if isinstance(which_socket, str):
            which_socket = name2id(which_socket)
        validate(which_socket)
        return sockets[which_socket].state

    # @return_with_status
    @staticmethod
    def startup():
        for index, socket in enumerate(sockets):
            Power.power(index, PowerState.On)
            time.sleep(2)

    # @return_with_status
    @staticmethod
    def shutdown():
        for index, socket in enumerate(sockets):
            Power.power(index, PowerState.Off)
            time.sleep(2)

    @staticmethod
    def status():
        return PowerStatus()

    @staticmethod
    def is_on(which_socket: int | str) -> bool:
        return Power.state(which_socket) == PowerState.On

    @staticmethod
    def all_on():
        for i in range(len(sockets)):
            Power.power(i, PowerState.On)

    @staticmethod
    def all_off():
        for i in range(len(sockets)):
            Power.power(i, PowerState.Off)


def validate(socket_id: int):
    if socket_id < 0 or socket_id > len(sockets):
        raise f'Invalid socket_id={socket_id}.  Must be [0..{len(sockets)}]'


def name2id(name: str) -> int:
    for index, socket in enumerate(sockets):
        if name == socket.name:
            return index
    return -1
