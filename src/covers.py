import win32com.client
import logging
from enum import IntFlag, Enum
from typing import TypeAlias, List

from common.utils import return_with_status, RepeatTimer, init_log, TimeStamped, Component
from common.ascom import AscomDriverInfo, ascom_run
from common.config import Config
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from mastapi import Mastapi


CoversStateType: TypeAlias = "CoversState"


class CoverActivities(IntFlag):
    Idle = 0
    Opening = (1 << 0)
    Closing = (1 << 1)
    StartingUp = (1 << 2)
    ShuttingDown = (1 << 3)


class CoversStatus(TimeStamped):
    is_on: bool
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


class Covers(Mastapi, Component, SwitchedPowerDevice):
    """
    Uses the PlaneWave ASCOM driver for the **MAST** mirror covers
    """

    def __init__(self, driver: str):
        self.conf: dict = Config().toml['covers']
        self.logger: logging.Logger = logging.getLogger('mast.unit.covers')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)
        Component.__init__(self)

        self.timer: RepeatTimer = RepeatTimer(2, self.ontimer)
        self.timer.name = 'covers-timer-thread'
        self.timer.start()

        self._connected: bool = False

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
        # if self.ascom:
        #     return self.ascom.Connected
        # else:
        #     return False
        return self._connected  # TODO: remove me

    @connected.setter
    def connected(self, value):
        self.logger.info(f"connected = {value}")
        try:
            ascom_run(self, f'Connected = {value}')
            self._connected = value     # TODO: remove me
        except Exception as ex:
            if (hasattr(ex, "excepinfo") and ex.excepinfo[1] == "PWShutter_ASCOM" and
                    ex.excepinfo[2] == "Unable to connect to PWShutter: got error code 255"):
                pass
            else:
                self.logger.error(f"failed to set connected to '{value}'", exc_info=ex)
                ascom_run(self, f'Connected = {value}')

    def state(self) -> CoversState:
        return CoversState(ascom_run(self, 'CoverState'))

    def status(self) -> CoversStatus:
        """
        :mastapi:
        """
        st = CoversStatus()
        st.reasons = list()
        st.ascom = AscomDriverInfo(self.ascom)
        st.state = self.state()
        st.is_on = self.is_on()
        if self.is_on():
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
        st.stamp()
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
        self.start_activity(CoverActivities.Opening)
        ascom_run(self, 'OpenCover()')

    @return_with_status
    def close(self):
        """
        Starts closing the **MAST** mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        self.logger.info('closing covers')
        self.start_activity(CoverActivities.Closing)
        ascom_run(self, 'CloseCover()')

    @return_with_status
    def startup(self):
        """
        Performs the ``startup`` routine for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        if self.state() != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp)
            self.open()

    @return_with_status
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.connected:
            return

        self.start_activity(CoverActivities.ShuttingDown)
        if self.state() != CoversState.Closed:
            self.close()

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        ascom_run(self, 'HaltCover()')
        for activity in (CoverActivities.StartingUp, CoverActivities.ShuttingDown,
                         CoverActivities.Closing, CoverActivities.Opening):
            if self.is_active(activity):
                self.end_activity(activity)

    def ontimer(self):
        if not self.connected:
            return

        # self.logger.debug(f"activities: {self.activities}, state: {self.state()}")
        if self.is_active(CoverActivities.Opening) and self.state() == CoversState.Open:
            self.end_activity(CoverActivities.Opening)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp)

        if self.is_active(CoverActivities.Closing) and self.state() == CoversState.Closed:
            self.end_activity(CoverActivities.Closing)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown)
                self.power_off()

    @property
    def name(self) -> str:
        return 'covers'

    @property
    def operational(self) -> bool:
        return True  # ?!?

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        return ret
