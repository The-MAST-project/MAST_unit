import datetime
import io
import os
from itertools import chain
import logging
import socket
import numpy as np
import camera
from PlaneWave import pwi4_client
import time
from typing import List, Any
from camera import Camera, CameraBinning, ExposurePurpose
from covers import Covers
from stage import Stage
from mount import Mount
from focuser import Focuser
from dlipower.dlipower.dlipower import SwitchedPowerDevice, PowerSwitchFactory
from common.utils import RepeatTimer
from threading import Thread
from common.utils import Component, BASE_UNIT_PATH, UnitRoi
from common.mast_logging import DailyFileHandler
from common.utils import time_stamp, CanonicalResponse, CanonicalResponse_Ok, function_name, OperatingMode
from common.filer import Filer
from common.config import Config
from common.activities import UnitActivities, FocuserActivities, CameraActivities
from common.activities import CoverActivities, StageActivities, MountActivities
from enum import Enum, auto
from fastapi.routing import APIRouter
from PIL import Image
import ipaddress
from starlette.websockets import WebSocket, WebSocketDisconnect

from autofocusing import Autofocuser, AutofocusResult
from solving import Solver
from acquiring import Acquirer
from guiding import Guider

logger = logging.getLogger('mast.unit')


class GuideDirections(Enum):
    guideNorth = 0
    guideSouth = 1
    guideEast = 2
    guideWest = 3


class Unit(Component):

    MAX_UNITS = 20
    MAX_AUTOFOCUS_TRIES = 3

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Unit, cls).__new__(cls)
            logger.info(f"Unit.__new__: allocated instance 0x{id(cls._instance):x}")
        return cls._instance

    def __init__(self, id_: int | str):
        if self._initialized:
            return
        logger.info(f"Unit.__init__: initiating instance 0x{id(self):x}")

        Component.__init__(self)

        self.operating_mode = OperatingMode.Night
        if 'UNIT_OPERATING_MODE' in os.environ:
            self.operating_mode = OperatingMode.Day if os.environ['UNIT_OPERATING_MODE'].lower() == 'day' \
                else OperatingMode.Night

        self._connected: bool = False

        self.was_tracking_before_guiding: bool = False

        file_handler = [h for h in logger.handlers if isinstance(h, DailyFileHandler)]
        logger.info(f"logging to '{file_handler[0].path}'")

        if isinstance(id_, int) and not 1 <= id_ <= Unit.MAX_UNITS:
            raise f"Bad unit id '{id_}', must be in [1..{Unit.MAX_UNITS}]"

        self.id = id_
        self.unit_conf = Config().get_unit()

        self.min_ra_correction_arcsec: float = float(self.unit_conf['guiding']['min_ra_correction_arcsec']) \
            if 'min_ra_correction_arcsec' in self.unit_conf['guiding'] else 1
        self.min_dec_correction_arcsec: float = float(self.unit_conf['guiding']['min_dec_correction_arcsec']) \
            if 'min_dec_correction_arcsec' in self.unit_conf['guiding'] else 1

        self.autofocus_max_tolerance = self.unit_conf['autofocus']['max_tolerance']
        self.autofocus_try: int = 0

        self.operating_mode: OperatingMode = OperatingMode.Night

        self.hostname = socket.gethostname()
        try:
            self.power_switch = PowerSwitchFactory.get_instance(
                conf=self.unit_conf['power_switch'],
                upload_outlet_names=True)
            self.mount: Mount = Mount(self.operating_mode)
            self.camera: Camera = Camera(self.operating_mode)
            self.covers: Covers = Covers()
            self.stage: Stage = Stage()
            self.focuser: Focuser = Focuser()
            self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()

            self.autofocuser: Autofocuser = Autofocuser(self)
            self.solver: Solver = Solver(self)
            self.acquirer: Acquirer = Acquirer(self)
            self.guider: Guider = Guider(self)
        except Exception as ex:
            logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.components: List[Component] = [
            self.power_switch,
            self.mount,
            self.camera,
            self.covers,
            self.focuser,
            self.stage,
        ]

        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer-thread'
        self.timer.start()

        self.reference_image = None
        self.autofocus_result: AutofocusResult | None = None

        self._was_shut_down = False

        self.connected_clients: List[WebSocket] = []
        # self.camera.register_visualizer('image-to-dashboard', self.push_image_to_dashboards)

        self.errors: List[str] = []

        # self.latest_solver_result: PS3SolvingResult | None = None

        self._initialized = True
        logger.info("unit: initialized")

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp)
        [comp.startup() for comp in self.components]

    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.

        Returns
        -------

        :mastapi:
        """
        if self.is_active(UnitActivities.StartingUp):
            return

        self._was_shut_down = False
        Thread(name='unit-startup-thread', target=self.do_startup).start()
        return CanonicalResponse_Ok

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown)
        [comp.shutdown() for comp in self.components]
        self._was_shut_down = True

    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.

        :mastapi:
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name='shutdown-thread', target=self.do_shutdown).start()
        return CanonicalResponse_Ok

    @property
    def connected(self):
        return all([comp.connected for comp in self.components])

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting

        """
        self.mount.connected = value
        self.camera.connected = value
        self.covers.connected = value
        self.stage.connected = value
        self.focuser.connected = value

    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.

        :mastapi:
        """
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    def power_all_on(self):
        """
        Turn **ON** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedPowerDevice):
                c.power_on()

    def power_all_off(self):
        """
        Turn **OFF** all power sockets

        :mastapi:
        """
        for c in self.components:
            if isinstance(c, SwitchedPowerDevice):
                c.power_off()

    def status(self) -> dict:
        """
        Returns
        -------
        UnitStatus
        :mastapi:
        """
        ret = self.component_status()
        ret |= {
            'id': id(self),
            'guiding': self.guider.guiding,
            'autofocusing': self.autofocuser.is_autofocusing,
        }
        for comp in self.components:
            ret[comp.name] = comp.status()
        time_stamp(ret)

        if self.autofocus_result:
            ret['autofocus'] = {
                'success': self.autofocus_result.success,
                'best_position': self.autofocus_result.best_position,
                'tolerance': self.autofocus_result.tolerance,
                'time_stamp': self.autofocus_result.time_stamp
            }

        ret['powered'] = True
        ret['type'] = 'full'
        # return ret
        return serialize_ip_addresses(ret)

    @staticmethod
    def quit():
        """
        Quits the application

        :mastapi:
        Returns
        -------

        """
        from app import app_quit
        app_quit()

    def abort(self):
        """
        Aborts any in-progress mount activity

        :mastapi:
        Returns
        -------

        """
        if self.is_active(UnitActivities.Guiding):
            self.guider.stop_guiding()
            while self.is_active(UnitActivities.Guiding):
                time.sleep(.2)

        if self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(UnitActivities.AutofocusingWIS):
            self.autofocuser.stop_autofocus()
            while self.is_active(UnitActivities.AutofocusingPWI4) or self.is_active(UnitActivities.AutofocusingWIS):
                time.sleep(.2)

        [component.abort() for component in self.components]
        # if self.is_active(UnitActivities.StartingUp):
        #     self.mount.abort()
        #     self.camera.abort()
        #     self.focuser.abort()
        #     self.stage.abort()
        #     self.covers.abort()

    def ontimer(self):
        """
        Used in order to end activities that were started elsewhere in the code.

        Returns
        -------

        """
        # UnitActivities.StartingUp
        if self.is_active(UnitActivities.StartingUp):
            if not (self.mount.is_active(MountActivities.StartingUp) or
                    self.camera.is_active(CameraActivities.StartingUp) or
                    self.stage.is_active(StageActivities.StartingUp) or
                    self.focuser.is_active(FocuserActivities.StartingUp) or
                    self.covers.is_active(CoverActivities.StartingUp)):
                self.end_activity(UnitActivities.StartingUp)

        # UnitActivities.ShuttingDown
        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.mount.is_active(MountActivities.ShuttingDown) or
                    self.camera.is_active(CameraActivities.ShuttingDown) or
                    self.stage.is_active(StageActivities.ShuttingDown) or
                    self.focuser.is_active(FocuserActivities.ShuttingDown) or
                    self.covers.is_active(CoverActivities.ShuttingDown) or
                    self.mount.is_active(MountActivities.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown)
                self._was_shut_down = True

        # UnitActivities.AutofocusingPWI4
        if self.is_active(UnitActivities.AutofocusingPWI4):
            autofocus_status = self.pw.status().autofocus
            if not autofocus_status:
                logger.error('Empty PWI4 autofocus status')
            elif not autofocus_status.is_running:   # it's done
                logger.info('PWI4 autofocus ended, getting status.')
                self.autofocus_result = AutofocusResult()
                self.autofocus_result.success = autofocus_status.success
                if self.autofocus_result.success:
                    self.autofocus_result.best_position = autofocus_status.best_position
                    self.autofocus_result.tolerance = autofocus_status.tolerance

                    best_position = autofocus_status.best_position
                    self.unit_conf['focuser']['known_as_good_position'] = best_position
                    try:
                        Config().set_unit(self.hostname, self.unit_conf)
                        logger.info(f"autofocus: saved {best_position=} in the configuration for unit {self.hostname}.")
                        if autofocus_status.tolerance > self.autofocus_max_tolerance:
                            if self.autofocus_try < Unit.MAX_AUTOFOCUS_TRIES:
                                self.autofocus_try += 1
                                logger.info(f"autofocus: latest {autofocus_status.tolerance=} greater than" +
                                            f"{self.autofocus_max_tolerance=}, starting autofocus " +
                                            f"try #{self.autofocus_try}")
                                self.autofocuser.start_pwi4_autofocus()
                            else:
                                logger.info(f"autofocus: failed to reach {self.autofocus_max_tolerance=} " +
                                            f"in {Unit.MAX_AUTOFOCUS_TRIES=}")
                        else:
                            self.autofocus_try = 0

                    except Exception as e:
                        logger.exception("failed to save unit_conf for ['focuser']['know_as_good_position']",
                                         exc_info=e)
                else:
                    logger.error(f"PlaneWave autofocus failed")
                    self.autofocus_result.best_position = None
                    self.autofocus_result.tolerance = None
                self.autofocus_result.time_stamp = datetime.datetime.now().isoformat()

                self.end_activity(UnitActivities.AutofocusingPWI4)
            else:
                logger.info(f'PlaneWave autofocus in progress {self.autofocus_try=}')

    def end_lifespan(self):
        logger.info('unit end lifespan')
        self.shutdown()

    def start_lifespan(self):
        logger.debug('unit start lifespan')
        self.startup()

    @property
    def operational(self) -> bool:
        return all([c.operational for c in self.components])

    @property
    def why_not_operational(self) -> List[str]:
        return list(chain.from_iterable(c.why_not_operational for c in self.components))

    @property
    def name(self) -> str:
        return 'unit'

    @property
    def detected(self) -> bool:
        # return all([comp.detected for comp in self.components])
        return True

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    async def unit_visual_ws(self, websocket: WebSocket):
        logger.info(f"accepting on {websocket=} ...")
        await websocket.accept()
        self.connected_clients.append(websocket)
        logger.info(f"added {websocket} to self.connected_clients")
        try:
            while True:
                _ = await websocket.receive_text()
        except WebSocketDisconnect:
            self.connected_clients.remove(websocket)
            logger.info(f"removed {websocket} from self.connected_clients")

    async def push_image_to_dashboards(self, image: np.ndarray):
        transposed_image = np.transpose(image.astype(np.uint16))
        image_pil = Image.fromarray(transposed_image)
        with io.BytesIO() as output:
            image_pil.save(output, format="PNG")
            png_data = output.getvalue()

        for websocket in self.connected_clients:
            try:
                logger.info(f"pushing to {websocket.url=} ...")
                await websocket.send(png_data)
                # loop = asyncio.get_event_loop()
                # loop.run_until_complete(websocket.send(png_data))
            except Exception as e:
                logger.error(f"websocket.send error: {e}")

    def expose_roi(self,
                   seconds: float | str = 3,
                   fiber_x: int | str = 6000,
                   fiber_y: int | str = 2500,
                   width: int | str = 500,
                   height: int | str = 300,
                   binning: int | str = 1,
                   gain: int | str = 170) -> CanonicalResponse:

        Thread(name='expose-roi-thread', target=self.do_expose_roi,
               args=[seconds, fiber_x, fiber_y, width, height, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_expose_roi(self,
                      seconds: float | str = 3,
                      fiber_x: int | str = 6000,
                      fiber_y: int | str = 2500,
                      width: int | str = 1500,
                      height: int | str = 1300,
                      binning: int | str = 1,
                      gain: int | str = 170) -> CanonicalResponse:

        seconds = float(seconds) if isinstance(seconds, str) else seconds
        fiber_x = int(fiber_x) if isinstance(fiber_x, str) else fiber_x
        fiber_y = int(fiber_y) if isinstance(fiber_y, str) else fiber_y
        width = int(width) if isinstance(width, str) else width
        height = int(height) if isinstance(height, str) else height
        _binning = int(binning) if isinstance(binning, str) else binning
        gain = int(gain) if isinstance(gain, str) else gain

        if _binning not in [1, 2, 4]:
            return CanonicalResponse(errors=[f"bad {_binning=}, should be 1, 2 or 4"])

        unit_roi = UnitRoi(fiber_x, fiber_y, width, height)
        binning: CameraBinning = CameraBinning(_binning, _binning)
        context = camera.CameraSettings(
            seconds=seconds,
            purpose=ExposurePurpose.Exposure,
            gain=gain,
            binning=binning,
            roi=unit_roi.to_camera_roi(binning=binning),
            tags={'expose-roi': None},
            save=True)
        self.camera.do_start_exposure(context)
        self.camera.wait_for_image_saved()
        Filer().move_ram_to_shared(self.camera.latest_settings.image_path)
        return CanonicalResponse_Ok

    def test_stage_repeatability(self,
                                 start_position: int | str = 50000,
                                 end_position: int | str = 300000,
                                 step: int | str = 25000,
                                 exposure_seconds: int | str = 5,
                                 binning: int | str = 1,
                                 gain: int | str = 170) -> CanonicalResponse:
        Thread(name='test-stage-repeatability', target=self.do_test_stage_repeatability,
               args=[start_position, end_position, step, exposure_seconds, binning, gain]).start()
        return CanonicalResponse_Ok

    def do_test_stage_repeatability(self,
                                    start_position: int | str = 50000,
                                    end_position: int | str = 300000,
                                    step: int | str = 25000,
                                    exposure_seconds: int | str = 5,
                                    binning: int | str = 1,
                                    gain: int | str = 170) -> CanonicalResponse:
        op = function_name()

        if isinstance(start_position, str):
            start_position = int(start_position)
        if isinstance(end_position, str):
            end_position = int(end_position)
        if isinstance(step, str):
            step = int(step)
        if isinstance(exposure_seconds, str):
            exposure_seconds = int(exposure_seconds)
        if isinstance(binning, str):
            binning = int(binning)
        if isinstance(gain, str):
            gain = int(gain)

        reference_position = start_position

        for position in range(start_position + step, end_position, step):
            logger.info(f"{op}: moving stage to {reference_position=}")
            self.stage.move_absolute(reference_position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            # expose at reference
            exposure_settings = camera.CameraSettings(
                seconds=exposure_seconds,
                gain=gain,
                binning=CameraBinning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "reference-for": position,
                }, save=True)

            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: reference image was saved")
            Filer().move_ram_to_shared(exposure_settings.image_path)

            # expose at shifted position
            logger.info(f"{op}: moving stage to shifted {position=}")
            self.stage.move_absolute(position)
            while self.stage.is_active(StageActivities.Moving):
                time.sleep(.5)

            exposure_settings = camera.CameraSettings(
                seconds=exposure_seconds,
                gain=gain,
                binning=CameraBinning(binning, binning),
                roi=None,
                tags={
                    "stage-repeatability": None,
                    "position": position,
                },
                save=True)
            self.camera.do_start_exposure(exposure_settings)
            self.camera.wait_for_image_saved()
            logger.info(f"{op}: image at {position=} was saved")
            Filer().move_ram_to_shared(exposure_settings.image_path)

        logger.info(f"{op}: done.")
        return CanonicalResponse_Ok


def serialize_ip_addresses(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: serialize_ip_addresses(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [serialize_ip_addresses(item) for item in data]
    elif isinstance(data, ipaddress.IPv4Address):
        return str(data)
    else:
        return data


unit_id: int | str | None = None
hostname = socket.gethostname()
if hostname.startswith('mast'):
    try:
        unit_id = int(hostname[4:])
    except ValueError:
        unit_id = hostname[4:]
else:
    logger.error(f"Cannot figure out the MAST unit_id ({hostname=})")

base_path = BASE_UNIT_PATH
tag = 'Unit'

unit: Unit | None = None
if not unit:
    unit = Unit(id_=unit_id)


def unit_route(sub_path: str):
    return base_path + sub_path


router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=unit.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=unit.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=unit.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=unit.status)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=unit.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=unit.disconnect)
router.add_api_route(base_path + '/start_pwi4_autofocus', tags=[tag], endpoint=unit.autofocuser.start_pwi4_autofocus)
router.add_api_route(base_path + '/start_wis_autofocus', tags=[tag], endpoint=unit.autofocuser.start_wis_autofocus)
router.add_api_route(base_path + '/stop_autofocus', tags=[tag], endpoint=unit.autofocuser.stop_autofocus)
router.add_api_route(base_path + '/start_guiding_by_solving', tags=[tag], endpoint=unit.guider.start_guiding_by_solving)
router.add_api_route(base_path + '/start_guiding_by_phase_correlation', tags=[tag],
                     endpoint=unit.guider.endpoint_start_guiding_by_cross_correlation)
router.add_api_route(base_path + '/stop_guiding', tags=[tag], endpoint=unit.guider.stop_guiding)
router.add_api_route(base_path + '/acquire', tags=[tag], endpoint=unit.acquirer.acquire)
router.add_api_route(base_path + '/expose_roi', tags=[tag], endpoint=unit.expose_roi)
router.add_api_route(base_path + '/test_stage_repeatability', tags=[tag], endpoint=unit.test_stage_repeatability)
