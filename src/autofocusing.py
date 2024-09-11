from threading import Thread
from common.utils import function_name, init_log, PathMaker, Filer, CanonicalResponse_Ok
import logging
import time
import os
from typing import List
from PlaneWave.ps3cli_client import PS3CLIClient, PS3AutofocusResult
from camera import CameraSettings, CameraBinning, ExposurePurpose
from stage import StagePresetPosition
from focuser import FocuserActivities
from unit import UnitActivities, UnitRoi

logger = logging.getLogger('mast.unit.autofocusing')
init_log(logger)


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
                            target_ra: float | None,  # center of ROI
                            target_dec: float | None,  # center of ROI
                            exposure: float,  # seconds
                            start_position: int | None = None,  # when None, start from current position
                            ticks_per_step: int = 50,  # focuser ticks per step
                            number_of_images: int = 5,
                            binning: int = 1,
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
               target=self.do_wis_autofocus,
               args=[
                   target_ra, target_dec, exposure, start_position,
                   ticks_per_step, number_of_images, binning
               ]).start()

    def do_wis_autofocus(self,
                         exposure: float,  # seconds
                         target_ra: float | None = None,  # center of ROI
                         target_dec: float | None = None,  # center of ROI
                         start_position: int | None = None,  # when None, start from the known-as-good position
                         ticks_per_step: int = 50,  # focuser ticks per step
                         number_of_images: int = 5,
                         binning: int = 1,
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
        binning             - CameraBinning
        """
        op = function_name()

        self.unit.start_activity(UnitActivities.AutofocusingWIS)

        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        pw_status = self.unit.pw.status()
        if not pw_status.mount.is_tracking:
            logger.info(f"{op}: starting mount tracking")
            self.unit.pw.mount_tracking_on()

        if not target_ra or not target_dec:
            logger.info(f"{op}: no target position was supplied, not moving the mount")
        else:
            self.unit.mount.goto_ra_dec_j2000(target_ra, target_dec)

        if not start_position:
            start_position = self.unit.unit_conf['focuser']['known_as_good_position']
        focuser_position: int = start_position - ((number_of_images / 2) * ticks_per_step)
        self.unit.focuser.position = focuser_position

        logger.debug(f"{op}: Waiting for components (stage, mount and focuser) to stop moving ...")
        while (self.unit.stage.is_moving or
               self.unit.mount.is_slewing or
               self.unit.focuser.is_active(FocuserActivities.Moving)):
            time.sleep(.5)
        logger.debug(f"{op}: Components (stage, mount and focuser) stopped moving ...")
        if not self.unit.is_active(UnitActivities.AutofocusingWIS):
            logger.info("activity 'AutofocusingWIS' was stopped")
            return

        acquisition_conf: dict = self.unit.unit_conf['acquisition']
        unit_roi = UnitRoi(
            acquisition_conf['fiber_x'],
            acquisition_conf['fiber_y'],
            acquisition_conf['width'],
            acquisition_conf['height'],
        )
        _binning = CameraBinning(binning, binning)
        autofocus_settings = CameraSettings(
            seconds=exposure,
            purpose=ExposurePurpose.Autofocus,
            binning=_binning,
            roi=unit_roi.to_camera_roi(binning=_binning),
            gain=acquisition_conf['gain'],
            save=True,
        )
        autofocus_folder = PathMaker().make_autofocus_folder()

        files: List[str] = []
        for image_no in range(number_of_images):
            autofocus_settings.image_path = os.path.join(autofocus_folder, f"FOCUS{focuser_position:05}.fits")

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

        ps3_client = PS3CLIClient()
        ps3_client.connect('127.0.0.1', 9896)
        ps3_result = PS3AutofocusResult(ps3_client.analyze_focus(files))
        ps3_client.close()

        if ps3_result.has_solution:
            logger.info(f"{op}: ps3 found an autofocus solution with {ps3_result.best_focus_position=}")
            logger.info(f"{op}: moving focuser to best focus position ...")
            self.unit.focuser.position = ps3_result.best_focus_position
            logger.info(f"{op}: waiting for focuser to stop moving ...")
            while self.unit.focuser.is_active(FocuserActivities.Moving):
                time.sleep(.5)
            logger.info(f"{op}: focuser stopped moving")
        else:
            logger.error(f"{op}: ps3 could not find an autofocus solution !!!")

        Filer().move_ram_to_shared(files)

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
                logger.info("Cannot stop PlaneWave autofocus, it is not running")
                return
            self.unit.pw.request("/autofocus/stop")
            self.unit.end_activity(UnitActivities.AutofocusingPWI4)
            return CanonicalResponse_Ok

        elif self.unit.is_active(UnitActivities.AutofocusingWIS):
            self.unit.end_activity(UnitActivities.AutofocusingWIS)
            return CanonicalResponse_Ok
