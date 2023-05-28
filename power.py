
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

    def __init__(self, p: PowerType):
        self.sockets = []
        for index, socket in enumerate(p.sockets):
            self.sockets.append(SocketStatus(name=socket.name, state=socket.state))
            if socket.state != PowerState.On:
                self.is_operational = False


class Power:

    ip_address: str
    id: int
    sockets: list[Socket]

    def __init__(self, _id):
        self.id = _id
        self.sockets = list()
        self.sockets.append(Socket(name='Mount', state=PowerState.Off))
        self.sockets.append(Socket(name='Camera', state=PowerState.Off))
        self.sockets.append(Socket(name='Stage', state=PowerState.Off))
        self.sockets.append(Socket(name='Cover', state=PowerState.Off))

    @property
    def connected(self):
        # get the real number of sockets, names and states
        return True

    @connected.setter
    def connected(self, value):
        pass

    @return_with_status
    def connect(self):
        self.connected = True

    @return_with_status
    def disconnect(self):
        self.connected = False

    def name2id(self, name: str) -> int:
        for index, socket in enumerate(self.sockets):
            if name == socket.name:
                return index
        return -1

    @return_with_status
    def power(self, which_socket: int | str, wanted_state: PowerState):
        if isinstance(which_socket, str):
            which_socket = self.name2id(which_socket)
        self.validate(which_socket)
        self.sockets[which_socket].state = wanted_state

    def state(self, which_socket: int | str) -> PowerState:
        if isinstance(which_socket, str):
            which_socket = self.name2id(which_socket)
        self.validate(which_socket)
        return self.sockets[which_socket].state

    def validate(self, socket_id: int):
        if socket_id < 0 or socket_id > len(self.sockets):
            raise f'Invalid socket_id={socket_id}.  Must be [0..{len(self.sockets)}]'

    @return_with_status
    def startup(self):
        for index, socket in enumerate(self.sockets):
            self.power(index, PowerState.On)
            time.sleep(2)

    @return_with_status
    def shutdown(self):
        for index, socket in enumerate(self.sockets):
            self.power(index, PowerState.Off)
            time.sleep(2)

    def status(self):
        return PowerStatus(self)

    def is_on(self, which_socket: int | str) -> bool:
        return self.state(which_socket) == PowerState.On
