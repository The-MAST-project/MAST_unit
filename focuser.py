import win32com.client
from typing import TypeAlias
import logging
from enum import Flag
from utils import AscomDriverInfo, RepeatTimer, return_with_status, Activities, init_log
from powered_device import PoweredDevice, PowerState
from PlaneWave import pwi4_client

FocuserType: TypeAlias = "Focuser"


class FocuserActivities(Flag):
    Idle = 0
    Moving = (1 << 0)
    StartingUp = (1 << 1)
    ShuttingDown = (1 << 2)


class FocuserStatus:

    activities: FocuserActivities = FocuserActivities.Idle
    is_operational: bool
    reasons: list[str]

    def __init__(self, f: FocuserType):
        stat = f.pw.status()
        self.ascom = AscomDriverInfo(f.ascom)
        self.reasons = list()
        self.is_powered = f.is_powered
        if self.is_powered:
            self.is_connected = f.connected
            if not self.is_connected:
                self.is_operational = False
                self.reasons.append('not-connected')
            else:
                self.is_operational = True
                self.is_moving = stat.focuser.is_moving
                self.position = stat.focuser.position
        else:
            self.is_operational = False
            self.is_connected = False
            self.reasons.append('not-powered')
            self.reasons.append('not-connected')

        self.activities = f.activities
        self.activities_verbal = self.activities.name


class Focuser(Activities, PoweredDevice):

    logger: logging.Logger
    pw: pwi4_client
    activities: FocuserActivities = FocuserActivities.Idle
    timer: RepeatTimer

    def __init__(self, driver: str):
        self.logger = logging.getLogger('mast.unit.focuser')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        PoweredDevice.__init__(self, 'Focuser', self)

        self.pw = pwi4_client.PWI4()
        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'focuser-timer-thread'
        self.timer.start()
        self.logger.info('initialized')

    @return_with_status
    def startup(self):
        """
        :mastapi:
        """
        if not self.is_powered:
            self.power(PowerState.On)
        if not self.connected:
            self.connect()
        self.pw.focuser_enable()

    @return_with_status
    def shutdown(self):
        """
        :mastapi:
        """
        if self.connected:
            self.disconnect()
        self.pw.focuser_disable()
        if self.is_powered:
            self.power(PowerState.Off)

    @return_with_status
    def connect(self):
        """
        :mastapi:
        """
        self.connected = True

    @return_with_status
    def disconnect(self):
        """
        :mastapi:
        """
        self.connected = False

    @property
    def connected(self):
        stat = self.pw.status()
        return stat.focuser.is_enabled and self.ascom and self.ascom.Connected

    @connected.setter
    def connected(self, value):
        if value:
            self.pw.focuser_connect()
            self.pw.focuser_enable()
        else:
            self.pw.focuser_disconnect()
            self.pw.focuser_disable()

        if self.ascom:
            self.ascom.Connected = value

    @property
    def position(self):
        """
        :mastapi:
        """
        stat = self.pw.status()
        return stat.focuser.position

    @return_with_status
    def goto(self, position: int | str):
        """
        Sends the focuser to the specified position

        :mastapi:
        :param int position: Target position
        """
        if not self.is_powered:
            self.logger.info('Cannot goto - not-powered')
            return
        if not self.connected:
            self.logger.info('Cannot goto - not-connected')
            return

        if isinstance(position, str):
            position = int(position)
        self.start_activity(FocuserActivities.Moving, self.logger)
        self.pw.focuser_goto(position)

    def ontimer(self):
        stat = self.pw.status()

        if self.is_active(FocuserActivities.Moving) and not stat.focuser.is_moving:
            self.end_activity(FocuserActivities.Moving, self.logger)

    def status(self) -> FocuserStatus:
        """

        Returns
        -------
            FocuserStatus

        """
        return FocuserStatus(self)
