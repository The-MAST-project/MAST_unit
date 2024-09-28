import time
import typing
from logging import Logger

import win32com.client
import logging

from PlaneWave import pwi4_client
from typing import List
from common.utils import Component, time_stamp, BASE_UNIT_PATH, OperatingMode
from common.utils import RepeatTimer, CanonicalResponse, CanonicalResponse_Ok, function_name
from dlipower.dlipower.dlipower import SwitchedPowerDevice
from common.config import Config
from fastapi.routing import APIRouter
import math
from astropy.coordinates import SkyCoord, frame_transform_graph, Angle
from common.ascom import ascom_run, AscomDispatcher
from common.activities import MountActivities

logger = logging.getLogger('mast.unit.' + __name__)


class Mount(Component, SwitchedPowerDevice, AscomDispatcher):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Mount, cls).__new__(cls)
        return cls._instance

    @property
    def logger(self) -> Logger:
        return logger

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    def __init__(self, operating_mode: OperatingMode = OperatingMode.Night):
        if self._initialized:
            return

        self.operating_mode = operating_mode
        self.unit_conf: dict = Config().get_unit()
        self.conf = self.unit_conf['mount']
        SwitchedPowerDevice.__init__(self, power_switch_conf=self.unit_conf['power_switch'], outlet_name='Mount')
        Component.__init__(self)

        if not self.is_on():
            self.power_on()

        self._was_shut_down: bool = False
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
        self.target: str | tuple | None = None

        self._initialized = True
        logger.info('initialized')

    def connect(self):
        """
        Connects to the MAST mount controller
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects from the MAST mount controller
        :mastapi:
        """
        if self.is_on():
            self.connected = False
        return CanonicalResponse_Ok

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
                    logger.error(f"failed to ASCOM connect (failure='{response.failure}')")
                if not st.mount.is_connected:
                    self.pw.mount_connect()
                if not st.mount.axis0.is_enabled:
                    self.pw.mount_enable(0)
                if not st.mount.axis1.is_enabled:
                    self.pw.mount_enable(1)
                logger.info(f'connected = {value}, axes enabled')
            else:
                if st.mount.axis0.is_enabled:
                    self.pw.mount_disable(0)
                if st.mount.axis1.is_enabled:
                    self.pw.mount_disable(1)
                self.pw.mount_disconnect()
                response = ascom_run(self, 'Connected = False')
                if response.failed:
                    self.errors.append(response.failure)
                logger.info(f'connected = {value}, axes disabled, disconnected')
        except Exception as ex:
            logger.exception(ex)

    def startup(self):
        """
        Performs the MAST startup routine (power ON, fans on and find home)
        :mastapi:
        """
        if not self.connected:
            self.connect()
        if not self.detected:
            return
        self.start_activity(MountActivities.StartingUp)
        self._was_shut_down = False
        self.pw.request('/fans/on')
        self.find_home()
        return CanonicalResponse_Ok

    def shutdown(self):
        """
        Performs the MAST shutdown routine (fans off, park, power OFF)
        :mastapi:
        """
        if self.connected:
            self.disconnect()
        self.start_activity(MountActivities.ShuttingDown)
        self.pw.request('/fans/off')
        self.park()
        self.power_off()
        return CanonicalResponse_Ok

    def park(self):
        """
        Parks the MAST mount
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivities.Parking)
            self.pw.mount_park()
        return CanonicalResponse_Ok

    def find_home(self):
        """
        Tells the MAST mount to find it's HOME indexes
        :mastapi:
        """
        if self.connected:
            self.target = 'Home'
            self.start_activity(MountActivities.FindingHome)
            self.last_axis0_position_degrees = -99999
            self.last_axis1_position_degrees = -99999
            self.pw.mount_find_home()
        return CanonicalResponse_Ok

    def goto(self, primary_coord: float | str, secondary_coord: float | str, frame: str = 'icrs'):
        op = function_name()

        if not self.connected:
            msg = f"{op}: not connected"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        frame_names = frame_transform_graph.get_names()
        if frame not in frame_names:
            error = f"{op}: '{frame}' not in [{frame_names}]"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        if frame != 'icrs':
            try:
                j2000_coord = SkyCoord(primary_coord, secondary_coord, frame)
                primary_coord = j2000_coord.ra
                secondary_coord = j2000_coord.dec
            except Exception as e:
                error = f"{op}: {e}"
                logger.error(error)
                return CanonicalResponse(errors=error)

        try:
            self.pw.mount_goto_ra_dec_j2000(primary_coord, secondary_coord)
        except Exception as e:
            error = f"{op}: {e}"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        return CanonicalResponse_Ok

    def ontimer(self):
        if not self.connected:
            return

        status = self.pw.status()
        if self.is_active(MountActivities.FindingHome):
            if not status.mount.is_slewing:
                self.end_activity(MountActivities.FindingHome)
                self.target = None
                if self.is_active(MountActivities.StartingUp):
                    self.end_activity(MountActivities.StartingUp)

        if self.is_active(MountActivities.Parking):
            if not status.mount.is_slewing:
                self.end_activity(MountActivities.Parking)
                self.target = None
                if self.is_active(MountActivities.ShuttingDown):
                    self.end_activity(MountActivities.ShuttingDown)
                    self._was_shut_down = True
                    self.power_off()

        if self.is_active(MountActivities.Slewing) and not status.mount.is_slewing:
            self.end_activity(MountActivities.Slewing)
            self.target = None

    def status(self) -> dict:
        """
        Returns the ``mount`` subsystem status
        :mastapi:
        """
        ret = self.power_status() | self.ascom_status() | self.component_status()
        ret |= {
            'errors': self.errors,
        }

        target_verbal = None
        if isinstance(self.target, str):
            target_verbal = self.target
        elif isinstance(self.target, tuple):
            target_verbal = (f"[{Angle(self.target[0], unit='hour').to_string(unit='hour', sep=':', precision=3)}, " +
                             f"{Angle(self.target[1], unit='arcsec').to_string(unit='deg', sep=':', precision=3)}]")

        if self.connected:
            st = self.pw.status()
            ret['tracking'] = st.mount.is_tracking
            # integrate activities we may have not started
            if st.mount.is_tracking:
                ret['activities'] |= MountActivities.Tracking
            else:
                ret['activities'] &= ~MountActivities.Tracking
            if st.mount.is_slewing:
                ret['activities'] |= MountActivities.Slewing
            else:
                ret['activities'] &= ~MountActivities.Slewing
            ret['activities_verbal'] = ret['activities'].__repr__()

            ret['slewing'] = st.mount.is_slewing
            ret['axis0_enabled'] = st.mount.axis0.is_enabled,
            ret['axis1_enabled'] = st.mount.axis1.is_enabled,
            ret['ra_j2000_hours '] = st.mount.ra_j2000_hours
            ret['dec_j2000_degs '] = st.mount.dec_j2000_degs
            ret['ha_hours '] = st.site.lmst_hours - st.mount.ra_j2000_hours
            ret['lmst_hours '] = st.site.lmst_hours
            ret['target_verbal'] = target_verbal
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
        logger.info("started tracking")
        return CanonicalResponse_Ok

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
        logger.info("stopped tracking")
        return CanonicalResponse_Ok

    def goto_ra_dec_j2000(self, ra: float, dec: float):
        self.start_activity(MountActivities.Slewing)
        self.target = (ra, dec)
        self.pw.mount_goto_ra_dec_j2000(ra, dec)

    def goto_ra_dec_apparent(self, ra: float, dec: float):
        self.start_activity(MountActivities.Slewing)
        self.pw.mount_goto_ra_dec_apparent(ra, dec)

    def abort(self):
        """
        Aborts any in-progress mount activities

        :mastapi:
        Returns
        -------

        """
        for activity in (MountActivities.FindingHome, MountActivities.StartingUp, MountActivities.ShuttingDown,
                         MountActivities.Dancing, MountActivities.Slewing):
            if self.is_active(activity):
                self.end_activity(activity)
        self.pw.mount_stop()
        self.pw.mount_tracking_off()
        return CanonicalResponse_Ok

    @property
    def operational(self) -> bool:
        st = self.pw.status()
        return all([self.is_on(), self.detected, self.connected, not self.was_shut_down,
                    self.ascom, st.mount.is_connected, st.mount.axis0.is_enabled, st.mount.axis1.is_enabled])

    @property
    def is_slewing(self):
        return self.pw.status().mount.is_slewing

    @property
    def why_not_operational(self) -> List[str]:
        st = self.pw.status()
        label = f"{self.name}"
        ret = []
        if not self.is_on():
            ret.append(f"{label}: not powered")
        elif not self.detected:
            ret.append(f"{label}: (PWI4) not detected")
        elif self.was_shut_down:
            ret.append(f"{label}: shut down")
        else:
            if self.ascom:
                response = ascom_run(self, 'Connected')
                if response.succeeded and not response.value:
                    ret.append(f"{label}: (ASCOM) - not connected")
            else:
                ret.append(f"{label}: (ASCOM) - no handle")

            if not st.mount.is_connected:
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

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    def dance(self):
        coordinates = cone_coordinates_generator()
        logger.info(f"dance: starting to dance")
        self.start_activity(MountActivities.Dancing)
        self.find_home()
        for coord in coordinates:
            logger.info(f"dance: dancing to {coord=}")
            self.goto_ra_dec_j2000(coord[0], coord[1])
            time.sleep(2)   # let it start moving
            stat = self.pw.status()
            while stat.mount.is_slewing:
                time.sleep(2)
                stat = self.pw.status()
            logger.info(f"dance: resting 10 seconds at {coord=}")
            time.sleep(10)
        logger.info(f"dance: done dancing")
        self.find_home()
        self.end_activity(MountActivities.Dancing)


# Function to generate cone coordinates
def cone_coordinates_generator(steps=20, base_radius=30, rotation_axis_ra=0, rotation_axis_dec=60):
    cone_coordinates = []
    for i in range(steps):
        angle = i * 2 * math.pi / steps
        ra = rotation_axis_ra + base_radius * math.cos(angle)
        dec = rotation_axis_dec + base_radius * math.sin(angle)
        cone_coordinates.append((ra, dec))

    # Combine all steps
    return [(rotation_axis_ra, rotation_axis_dec)] + cone_coordinates


base_path = BASE_UNIT_PATH + "/mount"
tag = 'Mount'

mount = Mount()

router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=mount.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=mount.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=mount.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=mount.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=mount.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=mount.disconnect)
router.add_api_route(base_path + '/start_tracking', tags=[tag], endpoint=mount.start_tracking)
router.add_api_route(base_path + '/stop_tracking', tags=[tag], endpoint=mount.stop_tracking)
router.add_api_route(base_path + '/park', tags=[tag], endpoint=mount.park)
router.add_api_route(base_path + '/find_home', tags=[tag], endpoint=mount.find_home)
router.add_api_route(base_path + '/goto', tags=[tag], endpoint=mount.goto)
router.add_api_route(base_path + '/dance', tags=[tag], endpoint=mount.dance)
