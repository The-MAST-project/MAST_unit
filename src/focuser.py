import win32com.client
from typing import TypeAlias, List
import logging
from enum import IntFlag

from common.utils import RepeatTimer, return_with_status, init_log, TimeStamped, Component
from common.ascom import AscomDriverInfo, ascom_run
from common.config import Config
from PlaneWave import pwi4_client
from mastapi import Mastapi
from dlipower.dlipower.dlipower import SwitchedPowerDevice

FocuserType: TypeAlias = "Focuser"


class FocuserActivities(IntFlag):
    Idle = 0
    Moving = (1 << 0)
    StartingUp = (1 << 1)
    ShuttingDown = (1 << 2)


class FocuserStatus(TimeStamped):

    activities: FocuserActivities = FocuserActivities.Idle
    is_operational: bool
    reasons: list[str]

    def __init__(self, f: FocuserType):
        stat = f.pw.status()
        self.ascom = AscomDriverInfo(f.ascom)
        self.reasons = list()
        self.is_on = f.is_on()
        if self.is_on:
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
        self.stamp()


class Focuser(Mastapi, Component, SwitchedPowerDevice):

    logger: logging.Logger
    pw: pwi4_client
    activities: FocuserActivities = FocuserActivities.Idle
    timer: RepeatTimer

    def __init__(self, driver: str):
        self.conf = Config().toml['focuser']
        self.logger = logging.getLogger('mast.unit.focuser')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)

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
        if not self.is_on():
            self.power_on()
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
        if self.is_on():
            self.power_off()

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
            ascom_run(self, f'Connected = {value}', True)

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

        Parameters
        ----------
        position
            The target position

        :mastapi:
        """
        if not self.is_on():
            self.logger.info('Cannot goto - not-powered')
            return
        if not self.connected:
            self.logger.info('Cannot goto - not-connected')
            return

        if isinstance(position, str):
            position = int(position)
        self.start_activity(FocuserActivities.Moving)
        self.pw.focuser_goto(position)

    def abort(self):
        """
        Aborts any in-progress focuser activities

        :mastapi:
        Returns
        -------

        """
        if self.is_active(FocuserActivities.Moving):
            self.pw.focuser_stop()
            self.end_activity(FocuserActivities.Moving)

        if self.is_active(FocuserActivities.StartingUp):
            self.end_activity(FocuserActivities.StartingUp)

    def ontimer(self):
        stat = self.pw.status()

        if self.is_active(FocuserActivities.Moving) and not stat.focuser.is_moving:
            self.end_activity(FocuserActivities.Moving)

    def status(self) -> FocuserStatus:
        """

        Returns
        -------
            FocuserStatus

        """
        return FocuserStatus(self)

    @property
    def name(self) -> str:
        return 'focuser'

    @property
    def operational(self) -> bool:
        return self.is_on()

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        return ret
