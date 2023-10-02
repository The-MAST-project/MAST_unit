import datetime

import win32com.client
import logging
from enum import Enum, Flag
from typing import TypeAlias
from utils import AscomDriverInfo, return_with_status, Activities, RepeatTimer, init_log, TimeStamped
from powered_device import PoweredDevice
from mastapi import Mastapi


CoversStateType: TypeAlias = "CoversState"


class CoverActivities(Flag):
    Idle = 0
    Opening = (1 << 0)
    Closing = (1 << 1)
    StartingUp = (1 << 2)
    ShuttingDown = (1 << 3)


class CoversStatus(TimeStamped):
    is_powered: bool
    is_connected: bool
    is_operational: bool
    state: CoversStateType
    state_verbal: str
    reasons: list[str] = None
    activities: CoverActivities = CoverActivities.Idle


class CoversState(Enum):
    NotPresent = 0
    Closed = 1
    Moving = 2
    Open = 3
    Unknown = 4
    Error = 5


class Covers(Mastapi, Activities, PoweredDevice):
    """
    Uses the PlaneWave ASCOM driver for the **MAST** mirror covers
    """
    logger: logging.Logger
    ascom = None
    timer: RepeatTimer
    activities: CoverActivities = CoverActivities.Idle

    def __init__(self, driver: str):
        self.logger = logging.getLogger('mast.unit.covers')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        PoweredDevice.__init__(self, 'Covers', self)
        Activities.__init__(self)

        self.timer = RepeatTimer(2, self.ontimer)
        self.timer.name = 'covers-timer-thread'
        self.timer.start()

        self.logger.info('initialized')

    def connect(self):
        """
        Connects to the **MAST** mirror cover controller

        :mastapi:
        """
        self.connected = True

    def disconnect(self):
        """
        Disconnects from the **MAST** mirror cover controller
        :mastapi:
        """
        self.connected = False

    @property
    def connected(self):
        if self.ascom:
            return self.ascom.Connected
        else:
            return False

    @connected.setter
    def connected(self, value):
        self.logger.info(f"connected = {value}")
        try:
            self.ascom.Connected = value
        except Exception as ex:
            self.logger.error(f"failed to set connected to '{value}'", exc_info=ex)
            self.ascom.Connected = value

    def state(self) -> CoversState:
        return CoversState(self.ascom.CoverState)

    def status(self) -> CoversStatus:
        """
        :mastapi:
        """
        st = CoversStatus()
        st.reasons = list()
        st.ascom = AscomDriverInfo(self.ascom)
        st.state = self.state()
        st.is_powered = self.is_powered
        if self.is_powered:
            st.is_connected = self.connected
            st.is_operational = False
            if st.is_connected:
                st.is_operational = st.state == CoversState.Open
                if not st.is_operational:
                    st.reasons.append(f'state is {st.state} instead of {CoversState.Open}')
                st.state = self.state()
                st.state_verbal = st.state.name
                st.activities = self.activities
                st.activities_verbal = self.activities.name
            else:
                st.reasons.append('not-connected')
        else:
            st.is_connected = False
            st.is_operational = False
            st.reasons.append('not-powered')
            st.reasons.append('not-connected')
        st.timestamp()
        return st

    @return_with_status
    def open(self):
        """
        Starts opening the **MAST** mirror covers

        :mastapi:
        """
        if not self.connected:
            return

        self.logger.info('opening covers')
        self.start_activity(CoverActivities.Opening, self.logger)
        self.ascom.OpenCover()

    @return_with_status
    def close(self):
        """
        Starts closing the **MAST** mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        self.logger.info('closing covers')
        self.start_activity(CoverActivities.Closing, self.logger)
        self.ascom.CloseCover()

    @return_with_status
    def startup(self):
        """
        Performs the ``startup`` routine for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.is_powered:
            self.power_on()
        if not self.connected:
            self.connect()
        if self.state() != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp, self.logger)
            self.open()

    @return_with_status
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.connected:
            return

        self.start_activity(CoverActivities.ShuttingDown, self.logger)
        if self.state() != CoversState.Closed:
            self.close()

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        self.ascom.HaltCover()
        for activity in (CoverActivities.StartingUp, CoverActivities.ShuttingDown,
                         CoverActivities.Closing, CoverActivities.Opening):
            if self.is_active(activity):
                self.end_activity(activity, self.logger)

    def ontimer(self):
        if not self.connected:
            return

        # self.logger.debug(f"activities: {self.activities}, state: {self.state()}")
        if self.is_active(CoverActivities.Opening) and self.state() == CoversState.Open:
            self.end_activity(CoverActivities.Opening, self.logger)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp, self.logger)

        if self.is_active(CoverActivities.Closing) and self.state() == CoversState.Closed:
            self.end_activity(CoverActivities.Closing, self.logger)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown, self.logger)
                self.power_off()
