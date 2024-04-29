import time
from logging import Logger

import win32com.client
import logging

from PlaneWave import pwi4_client
from typing import List
from enum import IntFlag, auto
from common.utils import init_log, Component, time_stamp
from common.utils import RepeatTimer, CanonicalResponse
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from common.config import Config
from common.ascom import ascom_driver_info, ascom_run, AscomDispatcher
from common.networking import NetworkedDevice
from mastapi import Mastapi


class MountActivities(IntFlag):
    StartingUp = auto()
    ShuttingDown = auto()
    Slewing = auto()
    Parking = auto()
    Tracking = auto()
    FindingHome = auto()


class Mount(Mastapi, Component, SwitchedPowerDevice, NetworkedDevice, AscomDispatcher):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Mount, cls).__new__(cls)
        return cls._instance

    def logger(self) -> Logger:
        return self._logger

    def ascom(self):
        return self._ascom

    def __init__(self):
        self.conf: dict = Config().toml['mount']
        self._logger: logging.Logger = logging.getLogger('mast.unit.mount')
        init_log(self._logger)

        SwitchedPowerDevice.__init__(self, self.conf)
        Component.__init__(self)
        NetworkedDevice.__init__(self, self.conf)

        self.last_axis0_position_degrees: int = -99999
        self.last_axis1_position_degrees: int = -99999
        self.default_guide_rate_degs_per_second = 0.002083  # degs/sec
        self.guide_rate_degs_per_second: float
        self.guide_rate_degs_per_ms: float

        self.pw: pwi4_client = pwi4_client.PWI4()
        self._ascom = win32com.client.Dispatch('ASCOM.PWI4.Telescope')
        #
        # Starting with PWI4 version 4.0.99 beta 22 it will be possible to query the ASCOM driver about
        #  the GuideRate for RightAscension and Declination.  The DriverVersion shows 1.0 (disregarding the PWI4
        #  version) so we need to use the default rate.
        #
        self.guide_rate_degs_per_second = self.default_guide_rate_degs_per_second
        self.guide_rate_degs_per_ms = self.guide_rate_degs_per_second / 1000
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'mount-timer-thread'
        self.timer.start()

        self.errors = []

        self._logger.info('initialized')

    def connect(self):
        """
        Connects to the MAST mount controller
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse.ok

    def disconnect(self):
        """
        Disconnects from the MAST mount controller
        :mastapi:
        """
        if self.is_on():
            self.connected = False
        return CanonicalResponse.ok

    @property
    def connected(self) -> bool:
        st = self.pw.status()
        response = ascom_run(self, 'Connected', True)
        return (self.ascom and
                (response.succeeded and response.value) and
                st.mount.is_connected and
                st.mount.axis0.is_enabled and
                st.mount.axis1.is_enabled)

    @connected.setter
    def connected(self, value):
        self.errors = []
        if not self.is_on():
            self.errors.append(f"not powered")
            return

        st = self.pw.status()
        try:
            if value:
                response = ascom_run(self, 'Connected = True')
                if response.failed:
                    self.errors.append(f"could not ASCOM connect")
                    self._logger.error(f"failed to ASCOM connect (failure='{response.failure}')")
                if not st.mount.is_connected:
                    self.pw.mount_connect()
                if not st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    self.pw.mount_enable(0)
                    self.pw.mount_enable(1)
                self._logger.info(f'connected = {value}, axes enabled')
            else:
                if st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    st.pw.mount_disable()
                st.pw.mount_disconnect()
                response = ascom_run(self, 'Connected = False')
                if response.failed:
                    self.errors.append(response.failure)
                self._logger.info(f'connected = {value}, axes disabled, disconnected')
        except Exception as ex:
            self._logger.exception(ex)

    def startup(self):
        """
        Performs the MAST startup routine (power ON, fans on and find home)
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self.start_activity(MountActivities.StartingUp)
        self.pw.request('/fans/on')
        self.find_home()
        return CanonicalResponse.ok

    def shutdown(self):
        """
        Performs the MAST shutdown routine (fans off, park, power OFF)
        :mastapi:
        """
        if not self.connected:
            self.connect()
        self.start_activity(MountActivities.ShuttingDown)
        self.pw.request('/fans/off')
        self.park()
        self.power_off()
        return CanonicalResponse.ok

    def park(self):
        """
        Parks the MAST mount
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivities.Parking)
            self.pw.mount_park()
        return CanonicalResponse.ok

    def find_home(self):
        """
        Tells the MAST mount to find it's HOME indexes
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivities.FindingHome)
            self.last_axis0_position_degrees = -99999
            self.last_axis1_position_degrees = -99999
            self.pw.mount_find_home()
        return CanonicalResponse.ok

    def ontimer(self):
        if not self.connected:
            return

        status = self.pw.status()
        if self.is_active(MountActivities.FindingHome):
            if not status.mount.is_slewing:
                self.end_activity(MountActivities.FindingHome)
                if self.is_active(MountActivities.StartingUp):
                    self.end_activity(MountActivities.StartingUp)

        if self.is_active(MountActivities.Parking):
            if not status.mount.is_slewing:
                self.end_activity(MountActivities.Parking)
                if self.is_active(MountActivities.ShuttingDown):
                    self.end_activity(MountActivities.ShuttingDown)
                    self.power_off()

    def status(self) -> dict:
        """
        Returns the ``mount`` subsystem status
        :mastapi:
        """
        ret = {
            'powered': self.switch.detected and self.is_on(),
            'detected': self.detected,
            'address': self.conf['network']['address'],
            'ascom': ascom_driver_info(self.ascom),
            'connected': self.connected,
            'operational': self.operational,
            'why_not_operational': self.why_not_operational,
            'activities': self.activities,
            'activities_verbal': self.activities.__repr__(),
            'errors': self.errors,
        }

        if self.connected:
            st = self.pw.status()
            ret['tracking'] = st.mount.is_tracking
            ret['slewing'] = st.mount.is_slewing
            ret['axis0_enabled'] = st.mount.axis0.is_enabled,
            ret['axis1_enabled'] = st.mount.axis1.is_enabled,
            ret['ra_j2000_hours '] = st.mount.ra_j2000_hours
            ret['dec_j2000_degs '] = st.mount.dec_j2000_degs
            ret['ha_hours '] = st.site.lmst_hours - st.mount.ra_j2000_hours
            ret['lmst_hours '] = st.site.lmst_hours
            ret['fans'] = True,  # TBD

        time_stamp(ret)
        return ret

    def start_tracking(self):
        """
        Tell the ``mount`` to start tracking
        :mastapi:
        """
        if not self.connected:
            return

        self.pw.mount_tracking_on()
        time.sleep(1)
        st = self.pw.status()
        while not st.mount.is_tracking:
            time.sleep(1)
            st = self.pw.status()
        return CanonicalResponse.ok

    def stop_tracking(self):
        """
        Tell the ``mount`` to stop tracking
        :mastapi:
        """
        if not self.connected:
            return

        self.pw.mount_tracking_off()
        time.sleep(1)
        st = self.pw.status()
        while st.mount.is_tracking:
            time.sleep(1)
            st = self.pw.status()
        return CanonicalResponse.ok

    def abort(self):
        """
        Aborts any in-progress mount activities

        :mastapi:
        Returns
        -------

        """
        for activity in MountActivities.FindingHome, MountActivities.StartingUp, MountActivities.ShuttingDown:
            if self.is_active(activity):
                self.end_activity(activity)
        self.pw.mount_stop()
        self.pw.mount_tracking_off()
        return CanonicalResponse.ok

    @property
    def operational(self) -> bool:
        return self.is_on() and self.connected

    @property
    def why_not_operational(self) -> List[str]:
        st = self.pw.status()
        label = f"{self.name}"
        ret = []
        if not self.is_on():
            ret.append(f"{label}: not powered")
        else:
            if self.ascom:
                response = ascom_run(self, 'Connected')
                if response.succeeded and not response.value:
                    ret.append(f"{label}: (ASCOM) - not connected")
            else:
                ret.append(f"{label}: (ASCOM) - no handle")

            if st.mount.is_connected:
                ret.append(f"{label}: (PWI4) - not connected")
            else:
                if not st.mount.axis0.is_enabled:
                    ret.append(f"{label}: (PWI4) - axis0 not enabled")
                if not st.mount.axis1.is_enabled:
                    ret.append(f"{label}: (PWI4) - axis1 not enabled")
        return ret

    @property
    def name(self) -> str:
        return 'mount'

    @property
    def detected(self) -> bool:
        st = self.pw.status()
        return st.mount.is_connected
