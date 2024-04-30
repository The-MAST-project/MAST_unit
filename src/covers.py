from logging import Logger

import win32com.client
import logging
from enum import IntFlag, Enum, auto
from typing import List

from common.utils import RepeatTimer, init_log, Component, time_stamp, CanonicalResponse
from common.ascom import ascom_driver_info, ascom_run, AscomDispatcher
from common.config import Config
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from mastapi import Mastapi


class CoverActivities(IntFlag):
    Idle = 0
    Opening = auto()
    Closing = auto()
    StartingUp = auto()
    ShuttingDown = auto()


# https://ascom-standards.org/Help/Developer/html/T_ASCOM_DeviceInterface_CoverStatus.htm
class CoversState(Enum):
    NotPresent = 0
    Closed = 1
    Moving = 2
    Open = 3
    Unknown = 4
    Error = 5


class Covers(Mastapi, Component, SwitchedPowerDevice, AscomDispatcher):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Covers, cls).__new__(cls)
        return cls._instance
    """
    Uses the PlaneWave ASCOM driver for the **MAST** mirror covers
    """

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    @property
    def logger(self) -> Logger:
        return self._logger

    def __init__(self):
        self.conf: dict = Config().toml['covers']
        self._logger: logging.Logger = logging.getLogger('mast.unit.covers')
        init_log(self._logger)
        try:
            self._ascom = win32com.client.Dispatch(self.conf['ascom_driver'])
        except Exception as ex:
            self._logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)
        Component.__init__(self)

        if not self.is_on():
            self.power_on()

        self.timer: RepeatTimer = RepeatTimer(2, self.ontimer)
        self.timer.name = 'covers-timer-thread'
        self.timer.start()

        self._connected: bool = False
        self._has_been_shut_down = False

        self._logger.info('initialized')

    def connect(self):
        """
        Connects to the **MAST** mirror cover controller

        :mastapi:
        """
        response = ascom_run(self, 'Connected = True')
        if response.failed:
            self._logger.error(f"failed to connect (failure={response.failure})")
            self._connected = False
        else:
            self._connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        Disconnects from the **MAST** mirror cover controller
        :mastapi:
        """
        self.connected = False
        return CanonicalResponse.ok

    @property
    def connected(self):
        # if self.ascom:
        #     return self.ascom.Connected
        # else:
        #     return False
        return self._connected

    @connected.setter
    def connected(self, value):
        self._logger.info(f"connected = {value}")
        try:
            response = ascom_run(self, f'Connected = {value}')
            if response.succeeded:
                self._connected = value
        finally:
            self._connected = False

    @property
    def state(self) -> CoversState:
        response = ascom_run(self, 'CoverState')
        if response.succeeded:
            return CoversState(response.value)
        else:
            return CoversState.Error

    def status(self) -> dict:
        """
        :mastapi:
        """
        ret = {
            'powered': self.is_on(),
            'detected': self.detected,
            'ascom': ascom_driver_info(self.ascom),
            'connected': self.connected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'activities': self.activities,
            'activities_verbal': self.activities.__repr__(),
            'shut_down': self.shut_down,
            'state': self.state,
            'state_verbal': self.state.__repr__(),
        }
        time_stamp(ret)
        return ret

    def open(self):
        """
        Starts opening the **MAST** mirror covers

        :mastapi:
        """
        if not self.connected:
            return

        self._logger.info('opening covers')
        self.start_activity(CoverActivities.Opening)
        response = ascom_run(self, 'OpenCover()')
        if response.failed:
            self._logger.error(f"failed to open covers (failure='{response.failure}')")
        return CanonicalResponse.ok

    def close(self):
        """
        Starts closing the **MAST** mirror covers
        :mastapi:
        """
        if not self.connected:
            return

        self._logger.info('closing covers')
        self.start_activity(CoverActivities.Closing)
        response = ascom_run(self, 'CloseCover()')
        if response.failed:
            self._logger.error(f"failed to close covers (failure='{response.failure}')")
        return CanonicalResponse.ok

    def startup(self):
        """
        Performs the ``startup`` routine for the **MAST** mirror covers controller

        :mastapi:
        """
        self._has_been_shut_down = False
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        if self.connected and self.state != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp)
            self.open()
        return CanonicalResponse.ok

    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers controller

        :mastapi:
        """
        if not self.connected:
            self.power_off()
            return

        if self.state != CoversState.Closed:
            self.start_activity(CoverActivities.ShuttingDown)
            self.close()
        return CanonicalResponse.ok

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        response = ascom_run(self, 'HaltCover()')
        if response.failed:
            self._logger.error(f"failed to halt covers (failure='{response.failure}')")
        for activity in (CoverActivities.StartingUp, CoverActivities.ShuttingDown,
                         CoverActivities.Closing, CoverActivities.Opening):
            if self.is_active(activity):
                self.end_activity(activity)
        return CanonicalResponse.ok

    def ontimer(self):
        if not self.connected:
            return

        # self._logger.debug(f"activities: {self.activities}, state: {self.state()}")
        if self.is_active(CoverActivities.Opening) and self.state == CoversState.Open:
            self.end_activity(CoverActivities.Opening)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp)

        if self.is_active(CoverActivities.Closing) and self.state == CoversState.Closed:
            self.end_activity(CoverActivities.Closing)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown)
                self._has_been_shut_down = True
                self.power_off()

    @property
    def name(self) -> str:
        return 'covers'

    @property
    def operational(self) -> bool:
        return all([self.is_on(), self.detected, self.ascom, self.connected, self.state == CoversState.Open])

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        elif not self.detected:
            ret.append(f"{self.name}: not detected")
        else:
            if not self.ascom:
                ret.append(f"{self.name}: (ASCOM) - no handle")
            else:
                if not self.connected:
                    ret.append(f"{self.name}: (ASCOM) - not connected")
                else:
                    state = self.state
                    if self.state != CoversState.Open:
                        ret.append(f"{self.name}: not open (state='{state.name}')")
        return ret

    @property
    def detected(self) -> bool:
        return self.connected
    
    @property
    def shut_down(self) -> bool:
        return self._has_been_shut_down
    
