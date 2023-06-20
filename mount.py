import time

import win32com.client
import logging
from PlaneWave import pwi4_client
from typing import TypeAlias
from enum import Flag
from utils import Activities, RepeatTimer, AscomDriverInfo, return_with_status, init_log
from powered_device import PoweredDevice


MountType: TypeAlias = "Mount"


class MountActivity(Flag):
    Idle = 0
    StartingUp = (1 << 0)
    ShuttingDown = (1 << 1)
    Slewing = (1 << 2)
    Parking = (1 << 3)
    Tracking = (1 << 4)
    FindingHome = (1 << 5)


class MountStatus:

    def __init__(self, m: MountType):
        self.ascom = AscomDriverInfo(m.ascom)
        self.reasons = list()
        st = m.pw.status()
        self.is_powered = m.is_powered
        if self.is_powered:
            self.is_connected = m.connected
            self.is_operational = False
            if self.is_connected:
                self.is_operational = \
                    st.mount.axis0.is_enabled and \
                    st.mount.axis1.is_enabled
                if not self.is_operational:
                    reason = f'one of the axes is not enabled: ' + \
                        f'axis0={"enabled" if st.mount.axis0.is_enabled else "disabled"} ' + \
                        f'axis1={"enabled" if st.mount.axis1.is_enabled else "disabled"} '
                    self.reasons.append(reason)
                self.is_tracking = st.mount.is_tracking
                self.is_slewing = st.mount.is_slewing
                self.ra_j2000_hours = st.mount.ra_j2000_hours
                self.dec_j2000_degs = st.mount.dec_j2000_degs
                self.lmst_hours = st.site.lmst_hours
                self.ha_hours = self.lmst_hours - self.ra_j2000_hours
                self.activities = m.activities
                self.activities_verbal = self.activities.name
                if self.is_tracking:
                    self.activities |= MountActivity.Tracking
                if self.is_slewing:
                    self.activities |= MountActivity.Slewing
            else:
                self.reasons.append('not-connected')
        else:
            self.is_operational = False
            self.is_connected = False
            self.reasons.append('not-powered')
            self.reasons.append('not-connected')


class Mount(Activities, PoweredDevice):

    logger: logging.Logger
    pw: pwi4_client
    activities: MountActivity = MountActivity.Idle
    timer: RepeatTimer
    last_axis0_position_degrees: int = -99999
    last_axis1_position_degrees: int = -99999

    def __init__(self):
        self.logger = logging.getLogger('mast.unit.mount')
        init_log(self.logger)

        PoweredDevice.__init__(self, 'Mount', self)

        self.pw = pwi4_client.PWI4()
        self.ascom = win32com.client.Dispatch('ASCOM.PWI4.Telescope')
        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'mount-timer-thread'
        self.timer.start()
        self.logger.info('initialized')

    @return_with_status
    def connect(self):
        """
        Connects to the MAST mount controller
        :mastapi:
        """
        if self.is_powered:
            self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects from the MAST mount controller
        :mastapi:
        """
        if self.is_powered:
            self.connected = False

    @property
    def connected(self) -> bool:
        st = self.pw.status()
        return self.ascom and self.ascom.Connected and st.mount.is_connected and st.mount.axis0.is_enabled and st.mount.axis1.is_enabled

    @connected.setter
    def connected(self, value):
        if not self.is_powered:
            return

        st = self.pw.status()
        try:
            if value:
                if not self.ascom.Connected:
                    self.ascom.Connected = True
                if not st.mount.is_connected:
                    self.pw.mount_connect()
                if not st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    self.pw.mount_enable(0)
                    self.pw.mount_enable(1)
                self.logger.info(f'connected = {value}, axes enabled, fans on')
            else:
                if st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    st.pw.mount_disable()
                st.pw.mount_disconnect()
                self.pw.request('/fans/off')
                self.ascom.Connected = False
                self.logger.info(f'connected = {value}, axes disabled, disconnected, fans off')
        except Exception as ex:
            self.logger.exception(ex)

    @return_with_status
    def startup(self):
        """
        Performs the MAST startup routine (power ON, fans on and find home)
        :mastapi:
        """
        if not self.is_powered:
            self.power_on()
        if not self.connected:
            self.connect()
        self.start_activity(MountActivity.StartingUp, self.logger)
        self.pw.request('/fans/on')
        self.find_home()

    @return_with_status
    def shutdown(self):
        """
        Performs the MAST shutdown routine (fans off, park, power OFF)
        :mastapi:
        """
        if not self.connected:
            self.connect()
        self.start_activity(MountActivity.ShuttingDown, self.logger)
        self.pw.request('/fans/off')
        self.park()
        self.power_off()

    @return_with_status
    def park(self):
        """
        Parks the MAST mount
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivity.Parking, self.logger)
            self.pw.mount_park()

    @return_with_status
    def find_home(self):
        """
        Tells the MAST mount to find it's HOME indexes
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivity.FindingHome, self.logger)
            self.last_axis0_position_degrees = -99999
            self.last_axis1_position_degrees = -99999
            self.pw.mount_find_home()

    def ontimer(self):
        if not self.connected:
            return

        status = self.pw.status()
        if self.is_active(MountActivity.FindingHome):
            if not status.mount.is_slewing:
                self.end_activity(MountActivity.FindingHome, self.logger)
                if self.is_active(MountActivity.StartingUp):
                    self.end_activity(MountActivity.StartingUp, self.logger)

        if self.is_active(MountActivity.Parking):
            if not status.mount.is_slewing:
                self.end_activity(MountActivity.Parking, self.logger)
                if self.is_active(MountActivity.ShuttingDown):
                    self.end_activity(MountActivity.ShuttingDown, self.logger)

    def status(self) -> MountStatus:
        """
        Returns the ``mount`` subsystem status
        :mastapi:
        """
        return MountStatus(self)

    @return_with_status
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

    @return_with_status
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
