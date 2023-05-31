import win32com.client
import logging
from enum import Enum, Flag
from typing import TypeAlias
from utils import AscomDriverInfo, return_with_status, Activities, RepeatTimer
from power import Power, PowerState

logger = logging.getLogger('mast.unit.covers')

CoversStateType: TypeAlias = "CoversState"


class CoverActivities(Flag):
    Idle = 0
    Opening = (1 << 0)
    Closing = (1 << 1)
    StartingUp = (1 << 2)
    ShuttingDown = (1 << 3)


class CoversStatus:
    is_powered: bool
    is_connected: bool
    is_operational: bool
    state: CoversStateType
    state_verbal: str
    not_operational_because: list[str] = None
    activities: CoverActivities = CoverActivities.Idle


class CoversState(Enum):
    NotPresent = 0
    Closed = 1
    Moving = 2
    Open = 3
    Unknown = 4
    Error = 5


class Covers(Activities):
    """
    Uses the PlaneWave ASCOM driver for the mirror covers
    """

    ascom = None
    timer: RepeatTimer
    activities: Activities = CoverActivities.Idle

    def __init__(self, driver: str):
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            logger.exception(ex)
            raise ex

        self.timer = RepeatTimer(2, self.ontimer)
        self.timer.name = 'covers-timer'
        self.timer.start()

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
        if self.ascom:
            return self.ascom.Connected
        else:
            return False

    @connected.setter
    def connected(self, value):
        if self.ascom:
            self.ascom.Connected = value

    def state(self) -> CoversState:
        return CoversState(self.ascom.CoverState)

    def status(self) -> CoversStatus:
        """
        :mastapi:
        """
        st = CoversStatus()
        st.not_operational_because = list()
        st.ascom = AscomDriverInfo(self.ascom)
        st.state = self.state()
        st.is_powered = self.is_powered
        if self.is_powered:
            st.is_connected = self.connected
            st.is_operational = False
            if st.is_connected:
                st.is_operational = st.state == CoversState.Open
                if not st.is_operational:
                    st.not_operational_because.append(f'state is {st.state} instead of {CoversState.Open}')
                st.state = self.state()
                st.state_verbal = st.state.name
            else:
                st.not_operational_because.append('not-connected')
        else:
            st.is_connected = False
            st.is_operational = False
            st.not_operational_because.append('not-powered')
            st.not_operational_because.append('not-connected')
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
        self.start_activity(CoverActivities.Opening, logger)
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
        self.start_activity(CoverActivities.Closing, logger)
        self.ascom.CloseCover()

    @return_with_status
    def startup(self):
        """
        Performs the ``startup`` procedure for the ``MAST`` mirror covers controller
        :mastapi:
        """
        if not self.is_powered:
            Power.power('Covers', PowerState.On)
        if not self.connected:
            self.connect()
        if self.state() != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp, logger)
            self.open()

    @return_with_status
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the ``MAST`` mirror covers controller
        :mastapi:
        """
        if not self.connected:
            return

        self.start_activity(CoverActivities.ShuttingDown, logger)
        if self.state() != CoversState.Closed:
            self.close()

    def ontimer(self):
        if not self.connected:
            return

        if self.is_active(CoverActivities.Opening) and self.state() == CoversState.Open:
            self.end_activity(CoverActivities.Opening, logger)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp, logger)

        if self.is_active(CoverActivities.Closing) and self.state() == CoversState.Closed:
            self.end_activity(CoverActivities.Closing, logger)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown, logger)
