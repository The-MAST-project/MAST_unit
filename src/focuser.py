import datetime

import win32com.client
from typing import List
import logging
from enum import IntFlag, auto

from common.utils import RepeatTimer, return_with_status, init_log, Component
from common.ascom import AscomDriverInfo, ascom_run
from common.config import Config
from PlaneWave import pwi4_client
from mastapi import Mastapi
from dlipower.dlipower.dlipower import SwitchedPowerDevice


class FocuserActivities(IntFlag):
    Idle = 0
    Moving = auto()
    StartingUp = auto()
    ShuttingDown = auto()


class Focuser(Mastapi, Component, SwitchedPowerDevice):

    def __init__(self):
        self.conf = Config().toml['focuser']
        self.logger: logging.Logger = logging.getLogger('mast.unit.focuser')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(self.conf['AscomDriver'])
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        SwitchedPowerDevice.__init__(self, self.conf)

        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
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

    def status(self) -> dict:
        """

        Returns
        -------
            FocuserStatus

        """
        stat = self.pw.status()
        ret = {
            'powered': self.is_on(),
            'ascom': AscomDriverInfo(self.ascom),
            'connected': stat.is_connected,
            'activities': self.activities,
            'activities_verbal': self.activities.__repr__(),
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'moving': stat.focuser.is_moving,
            'time_stamp': datetime.datetime.now().isoformat()
        }
        return ret

    @property
    def name(self) -> str:
        return 'focuser'

    @property
    def operational(self) -> bool:
        connected = False
        if self.ascom:
            connected = ascom_run(self, f'Connected')
        return self.is_on() and self.ascom and connected

    @property
    def why_not_operational(self) -> List[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        return ret
