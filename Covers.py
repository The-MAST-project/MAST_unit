import win32com.client
import logging
from enum import Enum
from typing import TypeAlias

logger = logging.getLogger('mast.unit.covers')

CoversStateType: TypeAlias = "CoversState"


class CoversStatus:
    is_connected: bool
    state: CoversStateType
    state_verbal: str
    is_operational: bool


class CoversState(Enum):
    NotPresent = 0
    Closed = 1
    Moving = 2
    Open = 3
    Unknown = 4
    Error = 5


class Covers:
    """
    Uses the PlaneWave ASCOM driver for the mirror covers
    """

    ascom = None

    def __init__(self, driver: str):
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            logger.exception(ex)
            raise ex
        logger.info('initialized')

    @property
    def connected(self):
        return self.ascom.connected

    @connected.setter
    def connected(self, value):
        try:
            self.ascom.connected = value
            logger.info(f'connected = {value}')
        except Exception as ex:
            logger.exception(ex)
            raise ex

    def state(self) -> CoversState:
        return CoversState(self.ascom.CoverState)

    def status(self) -> CoversStatus:
        st = CoversStatus()
        st.state = self.state()
        st.state_verbal = st.state.name
        st.is_connected = self.connected
        st.is_operational = self.connected and st.state == CoversState.Open
        return st

    def open(self):
        logger.info('opening covers')
        self.ascom.OpenCover()

    def close(self):
        logger.info('closing covers')
        self.ascom.CloseCover()

    def startup(self):
        if self.state() != CoversState.Open:
            self.open()

    def shutdown(self):
        if self.state() != CoversState.Closed:
            self.close()
