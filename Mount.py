
import logging
from PlaneWave import pwi4_client
from typing import TypeAlias
from enum import Flag
from threading import Timer

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
        st = m.pw.status()
        self.is_operational = \
            st.mount.is_connected and \
            st.mount.axis0.is_enabled and \
            st.mount.axis1.is_enabled
        self.is_tracking = st.mount.is_slewing
        self.is_slewing = st.mount.is_slewing
        self.ra_j2000_hours = st.mount.ra_j2000_hours
        self.dec_j2000_degs = st.mount.dec_j2000_degs
        self.activities = m.activities
        self.activities_verbal = self.activities.name
        if self.is_tracking:
            self.activities |= MountActivity.Tracking
        if self.is_slewing:
            self.activities |= MountActivity.Slewing


class Mount:

    pw: pwi4_client
    activities: MountActivity = MountActivity.Idle
    timer: Timer
    last_axis0_position_degrees: int = -99999
    last_axis1_position_degrees: int = -99999

    def __init__(self):
        self.pw = pwi4_client.PWI4()
        self.timer = Timer(2, function=self.ontimer)
        self.timer.name = 'mount-timer'
        self.timer.start()
        logger.info('initialized')

    @property
    def connected(self) -> bool:
        st = self.pw.status()
        return st.mount.is_connected and st.mount.axis0.is_enabled and st.mount.axis1.is_enabled

    @connected.setter
    def connected(self, value):
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

    def startup(self):
        self.activities |= MountActivity.StartingUp
        logger.info('startup - started')
        self.pw.request('/fans/on')
        self.find_home()
        logger.info('startup - done')
        self.activities &= ~MountActivity.StartingUp

    def shutdown(self):
        self.activities |= MountActivity.ShuttingDown
        logger.info('shutdown - started')
        self.pw.request('/fans/off')
        self.park()
        logger.info('shutdown - done')
        self.activities &= ~MountActivity.ShuttingDown

    def park(self):
        self.activities |= MountActivity.Parking
        self.pw.mount_park()

    def find_home(self):
        self.activities |= MountActivity.FindingHome
        logger.info('find home - started')
        self.last_axis0_position_degrees = -99999
        self.last_axis1_position_degrees = -99999
        self.pw.mount_find_home()

    def ontimer(self):
        if self.activities & MountActivity.FindingHome:
            status = self.pw.status()
            delta_axis0_position_degrees = status.mount.axis0.position_degs - self.last_axis0_position_degrees
            delta_axis1_position_degrees = status.mount.axis1.position_degs - self.last_axis1_position_degrees

            self.last_axis0_position_degrees = status.mount.axis0.position_degs
            self.last_axis1_position_degrees = status.mount.axis1.position_degs

            if abs(delta_axis0_position_degrees) < 0.001 and abs(delta_axis1_position_degrees) < 0.001:
                self.activities &= ~MountActivity.FindingHome
                self.last_axis0_position_degrees = -99999
                self.last_axis1_position_degrees = -99999
                logger.info('find home - done')

        self.timer = Timer(2, function=self.ontimer)
        self.timer.name = 'mount-timer'
        self.timer.start()

    def status(self) -> MountStatus:
        return MountStatus(self)
