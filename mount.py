import time

import win32com.client
import logging
from PlaneWave import pwi4_client
from typing import TypeAlias
from enum import Flag
from utils import Activities, RepeatTimer, AscomDriverInfo, return_with_status
from power import Power

logger = logging.getLogger('mast.unit.mount')

MountType: TypeAlias = "Mount"


class MountActivity(Flag):
    Idle = 0
    StartingUp = (1 << 1)
    ShuttingDown = (1 << 2)
    Slewing = (1 << 3)
    Parking = (1 << 4)
    Tracking = (1 << 5)
    FindingHome = (1 << 6)


class MountStatus:

    def __init__(self, m: MountType):
        self.ascom = AscomDriverInfo(m.ascom)
        st = m.pw.status()
        self.is_powered = m.is_powered
        if self.is_powered:
            self.is_connected = m.connected
            if self.is_connected:
                self.is_operational = \
                    st.mount.axis0.is_enabled and \
                    st.mount.axis1.is_enabled
                self.is_tracking = st.mount.is_tracking
                self.is_slewing = st.mount.is_slewing
                self.ra_j2000_hours = st.mount.ra_j2000_hours
                self.dec_j2000_degs = st.mount.dec_j2000_degs
                self.activities = m.activities
                self.activities_verbal = self.activities.name
                if self.is_tracking:
                    self.activities |= MountActivity.Tracking
                if self.is_slewing:
                    self.activities |= MountActivity.Slewing
        else:
            self.is_operational = False
            self.is_connected = False


class Mount(Activities):

    pw: pwi4_client
    activities: MountActivity = MountActivity.Idle
    timer: RepeatTimer
    last_axis0_position_degrees: int = -99999
    last_axis1_position_degrees: int = -99999

    def __init__(self):
        self.pw = pwi4_client.PWI4()
        self.ascom = win32com.client.Dispatch('ASCOM.PWI4.Telescope')
        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'mount-timer'
        self.timer.start()
        logger.info('initialized')

    @property
    def is_powered(self):
        return Power.is_on('Mount')

    @return_with_status
    def connect(self):
        if self.is_powered:
            self.connected = True

    @return_with_status
    def disconnect(self):
        if self.is_powered:
            self.connected = False

    @property
    def connected(self) -> bool:
        st = self.pw.status()
        return st.mount.is_connected and st.mount.axis0.is_enabled and st.mount.axis1.is_enabled

    @connected.setter
    def connected(self, value):
        if not self.is_powered:
            return

        st = self.pw.status()
        try:
            if value:
                if not st.mount.is_connected:
                    self.pw.mount_connect()
                if not st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    self.pw.mount_enable(0)
                    self.pw.mount_enable(1)
                logger.info(f'connected = {value}, axes enabled, fans on')
            else:
                if st.mount.axis0.is_enabled or st.mount.axis1.is_enabled:
                    st.pw.mount_disable()
                st.pw.mount_disconnect()
                self.pw.request('/fans/off')
                logger.info(f'connected = {value}, axes disabled, disconnected, fans off')
        except Exception as ex:
            logger.exception(ex)

    @return_with_status
    def startup(self):
        if self.connected:
            self.start_activity(MountActivity.StartingUp, logger)
            self.pw.request('/fans/on')
            self.find_home()
            self.end_activity(MountActivity.StartingUp, logger)

    @return_with_status
    def shutdown(self):
        if self.connected:
            self.start_activity(MountActivity.ShuttingDown, logger)
            self.pw.request('/fans/off')
            self.park()
            self.end_activity(MountActivity.ShuttingDown, logger)

    @return_with_status
    def park(self):
        if self.connected:
            self.start_activity(MountActivity.Parking, logger)
            self.pw.mount_park()

    def find_home(self):
        if self.connected:
            self.start_activity(MountActivity.FindingHome, logger)
            self.last_axis0_position_degrees = -99999
            self.last_axis1_position_degrees = -99999
            self.pw.mount_find_home()

    def ontimer(self):
        if not self.connected:
            return

        if self.is_active(MountActivity.FindingHome) and self.ascom.Connected:
            status = self.pw.status()
            delta_axis0_position_degrees = status.mount.axis0.position_degs - self.last_axis0_position_degrees
            delta_axis1_position_degrees = status.mount.axis1.position_degs - self.last_axis1_position_degrees

            self.last_axis0_position_degrees = status.mount.axis0.position_degs
            self.last_axis1_position_degrees = status.mount.axis1.position_degs

            if abs(delta_axis0_position_degrees) < 0.001 and abs(delta_axis1_position_degrees) < 0.001:
                self.end_activity(MountActivity.FindingHome, logger)
                self.last_axis0_position_degrees = -99999
                self.last_axis1_position_degrees = -99999

        if self.is_active(MountActivity.Parking) and self.ascom.Connected:
            parking_ra = self.ascom.SiderealTime
            parking_dec = self.ascom.SiteLatitude
            if abs(self.ascom.RightAscension - parking_ra) <= 0.1 and abs(self.ascom.Declination - parking_dec) < 0.1:
                self.end_activity(MountActivity.Parking, logger)

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
