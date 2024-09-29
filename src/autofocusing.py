import datetime
from threading import Thread
from common.utils import function_name, CanonicalResponse_Ok
from common.paths import PathMaker
from common.mast_logging import init_log
from common.filer import Filer
from common.config import Config
import logging
import time
import os
from typing import List
from PlaneWave.ps3cli_client import PS3CLIClient
from camera import CameraSettings, CameraBinning
from stage import StagePresetPosition
from common.activities import UnitActivities, FocuserActivities
from common.utils import UnitRoi
from plotting import plot_autofocus_analysis

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class AutofocusResult:
    success: bool
    best_position: float | None
    tolerance: float | None
    time_stamp: str


class PS3FocusSample:

    def __init__(self, d: dict):
        self.is_valid: bool = d.get('is_valid', False)
        self.focus_position: float | None = d.get('focus_position', None)
        self.num_stars: int | None = d.get('num_stars', None)
        self.star_rms_diameter_pixels: float | None = d.get('star_rms_diameter_pixels', None)
        self.vcurve_star_rms_diameter_pixels: float | None = d.get('vcurve_star_rms_diameter_pixels', None)


class PS3FocusAnalysisResult:

    def __init__(self, d: dict):
        self.has_solution: bool = d.get('has_solution', False)
        self.best_focus_position: float | None = d.get('best_focus_position', None)
        self.best_focus_star_diameter: float | None = d.get('best_focus_star_diameter', None)
        self.tolerance: float | None = d.get('tolerance', None)
        self.vcurve_a: float | None = d.get('vcurve_a', None)
        self.vcurve_b: float | None = d.get('vcurve_b', None)
        self.vcurve_c: float | None = d.get('vcurve_c', None)
        self.focus_samples: List[PS3FocusSample] = []
        for s in d.get('focus_samples', []):
            self.focus_samples.append(PS3FocusSample(s))


class PS3AutofocusStatus:

    def __init__(self, d: dict):
        """
        Parses a dictionary into a PS3AutofocusStatus instance
        :param d:
        """

        self.is_running: bool = d.get('is_running', False)
        self.last_log_message: str | None = d.get('last_log_message', None)
        self.error_message: str | None = d.get('error_message', None)
        self.analysis_result = None
        d1 = d.get('analysis_result', None)
        if d1:
            self.analysis_result: PS3FocusAnalysisResult = PS3FocusAnalysisResult(d1)


class Autofocuser:
    
    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit

    @property
    def is_autofocusing(self) -> bool:
        """
        Returns the status of the ``autofocus`` routine
        """
        if not self.unit.connected:
            return False

        return (self.unit.is_active(UnitActivities.AutofocusingWIS) or
                (self.unit.is_active(UnitActivities.AutofocusingPWI4) and self.unit.pw.status().autofocus.is_running))

    def start_wis_autofocus(self,
                            target_ra: float | None = None,  # center of ROI
                            target_dec: float | None = None,  # center of ROI
                            exposure: float = 5,  # seconds
                            start_position: int | None = None,  # when None, start from current position
                            ticks_per_step: int = 50,  # focuser ticks per step
                            number_of_images: int = 5,
                            ):
        """

        Parameters
        ----------
        target_ra - if supplied start by sending the mount to these coordinates
        target_dec - if supplied start by sending the mount to these coordinates
        exposure - exposure duration in seconds
        start_position - if supplied start by sending the focuser to this position, else to the known-as-good position
        ticks_per_step - by how many ticks to increase the focuser position between exposures
        number_of_images - how many exposures to take, MUST be odd
        binning - the binning to use, defaults to 1x1

        Returns
        -------

        """
        if number_of_images % 2 != 1:
            raise Exception(f"number_of_images MUST be odd!")

        Thread(name='wis-autofocus',
               target=self.do_start_wis_autofocus,
               args=[
                   target_ra, target_dec, exposure, start_position,
                   ticks_per_step, number_of_images
               ]).start()

    def do_start_wis_autofocus(self,
                               target_ra: float | None = None,  # center of ROI
                               target_dec: float | None = None,  # center of ROI
                               exposure: float = 5,  # seconds
                               start_position: int | None = None,  # when None, start from the known-as-good position
                               ticks_per_step: int = 50,  # focuser ticks per step
                               number_of_images: int = 5,
                               ):
        """
        Use PlaneWave's new method for autofocus:
        - Move the stage to 'Sky'
        - Move the mount to (target_ra, target_dec), if supplied, otherwise stay where you are
        - Move the focuser to 'start_position', if supplied, otherwise the known-as-good position
        - Set the ROI as for the acquisition ROI
        - Take the exposures while moving the focuser by 'ticks_per_step' between images
        - Send the images to PWI4, get the results
        - TODO: Learn from the results whether more runs will get a better result, if 'yes': do so

        Parameters
        ----------
        target_ra           - Ra for telescope move
        target_dec          - Dec for telescope move
        exposure            - In seconds
        start_position      - Focuser staring position
        ticks_per_step      - Focuser steps between exposures
        number_of_images    - How many images to take
        """
        op = function_name()
        self.unit.errors = []

        self.unit.start_activity(UnitActivities.AutofocusingWIS)

        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        pw_status = self.unit.pw.status()
        if not pw_status.mount.is_tracking:
            logger.info(f"{op}: starting mount tracking")
            self.unit.pw.mount_tracking_on()

        if not target_ra or not target_dec:
            logger.info(f"{op}: no target position was supplied, not moving the mount")
        else:
            logger.info(f"{op}: moving mount to {target_ra=}, {target_dec=} ...")
            self.unit.mount.goto_ra_dec_j2000(target_ra, target_dec)

        if not start_position:
            start_position = self.unit.unit_conf['focuser']['known_as_good_position']
        focuser_position: int = start_position - ((number_of_images / 2) * ticks_per_step)
        self.unit.focuser.position = focuser_position

        logger.debug(f"{op}: Waiting for components (stage, mount, focuser) to stop moving ...")
        while (self.unit.stage.is_moving or
               self.unit.mount.is_slewing or
               self.unit.focuser.is_active(FocuserActivities.Moving)):
            time.sleep(.5)
        logger.debug(f"{op}: Components (stage, mount, focuser) stopped moving ...")
        if not self.unit.is_active(UnitActivities.AutofocusingWIS):
            logger.info("activity 'AutofocusingWIS' was stopped")
            return

        acquisition_conf: dict = self.unit.unit_conf['acquisition']
        unit_roi = UnitRoi(
            acquisition_conf['roi']['fiber_x'],
            acquisition_conf['roi']['fiber_y'],
            acquisition_conf['roi']['width'],
            acquisition_conf['roi']['height'],
        )
        _binning = CameraBinning(1, 1)
        autofocus_folder = PathMaker().make_autofocus_folder()

        max_tries: int = self.unit.unit_conf['autofocus']['max_tries']
        max_tolerance: float = self.unit.unit_conf['autofocus']['max_tolerance']
        try_number: int

        for try_number in range(max_tries):

            logger.info(f"{op}: starting autofocus try #{try_number} (of {max_tries})")
            #
            # Acquire images
            #
            files: List[str] = []
            for image_no in range(number_of_images):
                autofocus_settings = CameraSettings(
                    seconds=exposure,
                    binning=_binning,
                    roi=unit_roi.to_camera_roi(binning=_binning),
                    gain=acquisition_conf['gain'],
                    image_path=os.path.join(autofocus_folder, f"FOCUS{int(focuser_position):05}.fits"),
                    save=True,
                )

                logger.info(f"{op}: starting exposure #{image_no} of {number_of_images} at {focuser_position=} ...")
                self.unit.camera.do_start_exposure(autofocus_settings)
                logger.info(f"{op}: waiting for exposure #{image_no} of {number_of_images} ...")
                self.unit.camera.wait_for_image_saved()
                files.append(self.unit.camera.latest_settings.image_path)
                if not self.unit.is_active(UnitActivities.AutofocusingWIS):  # have we been stopped?
                    logger.info(f"{op}: activity 'AutofocusingWIS' was stopped")
                    return

                focuser_position += ticks_per_step
                logger.info(f"{op}: moving focuser by {ticks_per_step} ticks (to {focuser_position}) ...")
                self.unit.focuser.position = focuser_position
                while self.unit.focuser.is_active(FocuserActivities.Moving):
                    time.sleep(.5)
                logger.info(f"{op}: focuser stopped moving")

                if not self.unit.is_active(UnitActivities.AutofocusingWIS):  # have we been stopped?
                    logger.info(f"{op}: activity 'AutofocusingWIS' was stopped")
                    return

            # The files are now on the RAM disk

            self.unit.start_activity(UnitActivities.AutofocusAnalysis)
            ps3_client = PS3CLIClient()
            ps3_client.connect('127.0.0.1', 8998)
            ps3_client.begin_analyze_focus(files)

            status: PS3AutofocusStatus | None = None
            d: dict | None = None
            timeout = 60
            start = datetime.datetime.now()
            end = start + datetime.timedelta(seconds=timeout)
            while datetime.datetime.now() < end:
                # wait for the autofocus analyser to start running
                d = ps3_client.focus_status()
                if d is None:
                    time.sleep(.1)
                    continue
                status = PS3AutofocusStatus(d)
                if not status.is_running:
                    time.sleep(.1)
                else:
                    break
            if datetime.datetime.now() >= end:
                logger.error(f"{op}: autofocus analyser did not start within {timeout} seconds")
                Filer().move_ram_to_shared(files)
                self.unit.end_activity(UnitActivities.AutofocusAnalysis)
                self.unit.end_activity(UnitActivities.AutofocusingWIS)
                return

            while datetime.datetime.now() < end:
                # wait for the autofocus analyser to stop running
                s = ps3_client.focus_status()
                status: PS3AutofocusStatus = PS3AutofocusStatus(s)
                logger.info(f"{op}: {s=}")
                if not status.is_running:
                    break
                else:
                    time.sleep(.5)

            if datetime.datetime.now() >= end:
                logger.error(f"{op}: autofocus analyser did not finish within {timeout} seconds")
                ps3_client.close()
                Filer().move_ram_to_shared(files)
                self.unit.end_activity(UnitActivities.AutofocusAnalysis)
                continue  # next try_number

            ps3_client.close()
            self.unit.end_activity(UnitActivities.AutofocusAnalysis)

            if not status.analysis_result:
                logger.error(f"{op}: focus analyser stopped working but empty analysis_result")
                continue  # next try_number

            if not status.analysis_result.has_solution:
                logger.error(f"{op}: focus analyser did not find a solution")
                self.unit.end_activity(UnitActivities.AutofocusingWIS)
                continue  # next try_number

            #
            # We have an analysis solution
            #
            result: PS3FocusAnalysisResult = status.analysis_result
            logger.info(f"{op}: analysis result: " +
                        f"{result.best_focus_position=}, {result.best_focus_star_diameter=}, {result.tolerance=}")

            if result.tolerance > max_tolerance:
                logger.info(f"{op}: {result.tolerance=} is higher than {max_tolerance=}, ignoring this solution")
                continue  # next try_number

            position: int = int(result.best_focus_position)
            logger.info(f"{op}: moving focuser to best focus position {position} ...")
            self.unit.focuser.known_as_good_position = position
            self.unit.focuser.position = self.unit.focuser.known_as_good_position

            logger.info(f"{op}: waiting for focuser to stop moving ...")
            while self.unit.focuser.is_active(FocuserActivities.Moving):
                time.sleep(.5)
            logger.info(f"{op}: focuser stopped moving")

            self.unit.unit_conf['focuser']['known_as_good_position'] = position
            try:
                Config().set_unit(self.unit.hostname, self.unit.unit_conf)
                logger.info(f"saved unit '{self.unit.hostname}' configuration for " +
                            f"focuser known-as-good-position {position}")
            except Exception as e:
                logger.error(f"could not save unit '{self.unit.hostname}' " +
                             f"configuration for focuser known-as-good-position (exception: {e})")

            Filer().move_ram_to_shared(files)
            pixel_scale: float = self.unit.unit_conf['camera']['pixel_scale_at_bin1']
            Thread(name='autofocus-analysis-plotter', target=plot_autofocus_analysis,
                   args=[result, autofocus_folder, pixel_scale]).start()

            break  # the tries loop

        if try_number == max_tries - 1:
            logger.error(f"{op}: could not acieve {max_tolerance=} within {max_tries=}")

        self.unit.end_activity(UnitActivities.AutofocusingWIS)

    def start_pwi4_autofocus(self):
        """
        Starts the ``autofocus`` routine (implemented by _PlaneWave_)

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot start PlaneWave autofocus - not-connected')
        #     return

        if self.unit.pw.status().autofocus.is_running:
            logger.info("pwi4 autofocus already running")
            return

        #
        # NOTE: The PWI4 autofocus method uses the autofocus parameters set via the PWI4 GUI
        #

        self.unit.pw.request("/autofocus/start")
        while not self.unit.pw.status().autofocus.is_running:  # wait for it to actually start
            logger.debug('waiting for PlaneWave autofocus to start')
            time.sleep(1)
        if self.unit.autofocus_try == 0:
            self.unit.start_activity(UnitActivities.AutofocusingPWI4)
        logger.debug('PlaneWave autofocus has started')
        return CanonicalResponse_Ok

    def stop_autofocus(self):
        """
        Stops the ``autofocus`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.error('Cannot stop PlaneWave autofocus - not-connected')
        #     return

        if self.unit.is_active(UnitActivities.AutofocusingPWI4):
            if not self.unit.pw.status().autofocus.is_running:
                logger.info("Cannot stop PWI4 autofocus, it is not running")
                return
            self.unit.pw.request("/autofocus/stop")
            self.unit.end_activity(UnitActivities.AutofocusingPWI4)
            return CanonicalResponse_Ok

        elif self.unit.is_active(UnitActivities.AutofocusingWIS):
            self.unit.end_activity(UnitActivities.AutofocusingWIS)
            return CanonicalResponse_Ok
