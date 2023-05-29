import win32com.client
import logging
from enum import Enum
from typing import TypeAlias
from utils import AscomDriverInfo, return_with_status
from power import Power

logger = logging.getLogger('mast.unit.covers')

CoversStateType: TypeAlias = "CoversState"


class CoversStatus:
    is_powered: bool
    is_connected: bool
    is_operational: bool
    state: CoversStateType
    state_verbal: str


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
    _connected: bool = False

    def __init__(self, driver: str):
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            logger.exception(ex)
            raise ex

        logger.info('initialized')

    @property
    def is_powered(self):
        return Power.is_on('Covers')

    def connect(self):
        """
        Connects to the ``MAST`` mirror cover controller
        :mastapi:
        """
        if self.is_powered:
            self.connected = True

    def disconnect(self):
        """
        Disconnects from the ``MAST`` mirror cover controller
        :mastapi:
        """
        if self.is_powered:
            self.connected = False

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = value

    def state(self) -> CoversState:
        return CoversState(self.ascom.CoverState)

    def status(self) -> CoversStatus:
        """
        :mastapi:
        """
        st = CoversStatus()
        st.ascom = AscomDriverInfo(self.ascom)
        st.is_powered = self.is_powered
        if self.is_powered:
            st.is_connected = self.connected
            if st.is_connected:
                st.is_operational = st.state == CoversState.Open
                st.state = self.state()
                st.state_verbal = st.state.name
        else:
            st.is_connected = False
            st.is_operational = False
        return st

    @return_with_status
    def open(self):
        """
        Starts opening the ``MAST`` mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        logger.info('opening covers')
        self.ascom.OpenCover()

    @return_with_status
    def close(self):
        """
        Starts closing the ``MAST`` mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        logger.info('closing covers')
        self.ascom.CloseCover()

    @return_with_status
    def startup(self):
        """
        Performs the ``startup`` procedure for the ``MAST`` mirror covers controller
        :mastapi:
        """
        if not self.connected:
            return

        if self.state() != CoversState.Open:
            self.open()

    @return_with_status
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the ``MAST`` mirror covers controller
        :mastapi:
        """
        if not self.connected:
            return

        if self.state() != CoversState.Closed:
            self.close()
