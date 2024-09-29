import time
import logging
from common.utils import function_name, Coord
from common.paths import PathMaker
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi
from common.corrections import correction_phases
from stage import StagePresetPosition
from camera import CameraSettings, CameraBinning
from astropy.coordinates import Angle
import astropy.units as u
from solving import SolvingTolerance
from threading import Thread
from acquisition import Acquisition
import os

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class Acquirer:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit
        self.folder: str | None = None
        self.latest_acquisition: Acquisition | None = None

    def do_solve_and_correct(self,
                             target_ra_j2000_hours: float,
                             target_dec_j2000_degs: float,
                             phase: str | None = None):
        """
        Performs a sequence of: exposure, plate solving and telescope correction.
        :param target_ra_j2000_hours:
        :param target_dec_j2000_degs:
        :param phase: Used as part of the acquisition folder path.  Will be either 'sky', 'spec' or 'guiding', defaults
                       to 'testing' when not called from an acquisition phase
        :return:
        """
        op = function_name()
        acquisition_conf = self.unit.unit_conf['acquisition']

        self.unit.start_activity(UnitActivities.Positioning)
        self.unit.stage.move_to_preset(StagePresetPosition.Sky)
        self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
        while self.unit.stage.is_moving or self.unit.mount.is_slewing:
            time.sleep(1)
        logger.info(f"{op}: sleeping 10 seconds to let the mount stop ...")
        time.sleep(10)
        self.unit.end_activity(UnitActivities.Positioning)

        # Prepare camera settings
        if phase is None:
            phase = 'testing'

        if phase not in correction_phases:
            msg = f"{op}: bad phase {phase}, must be one of {','.join(correction_phases)}"
            logger.error(msg)
            raise Exception(msg)

        #
        # Possible folder names:
        #  .../<date>/Acquisitions/target=<ra>,<dec>,time<datetime>/{sky|spec|guiding|testing,00000X}
        #
        self.folder = PathMaker().make_acquisition_folder(
            phase=phase,
            tags={
                'target': f"{target_ra_j2000_hours},{target_dec_j2000_degs}",
            })
        if phase == 'testing':
            self.folder += ',' + PathMaker().make_seq(self.folder)
        self.folder = os.path.join(self.folder, phase)
        os.makedirs(self.folder, exist_ok=True)

        settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            base_folder=self.folder,
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        # Figure out tolerances
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        if 'tolerance' in acquisition_conf:
            if 'ra_arcsec' in acquisition_conf['tolerance']:
                ra_tolerance = Angle(acquisition_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in acquisition_conf['tolerance']:
                dec_tolerance = Angle(acquisition_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        if not self.unit.solver.solve_and_correct(target=target,
                                                  camera_settings=settings,
                                                  solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                  phase=phase,
                                                  parent_activity=UnitActivities.Acquiring,
                                                  max_tries=10):
            logger.info(f"{op}: solve_and_correct failed")
        logger.info(f"{op}: solve_and_correct done.")

    def do_acquire(self, target_ra_j2000_hours: float, target_dec_j2000_degs: float):
        """
        Called from start_acquisition()

        :param target_ra_j2000_hours:
        :param target_dec_j2000_degs:
        :return:
        """
        op = function_name()

        self.unit.errors = []
        self.unit.reference_image = None
        acquisition_conf = self.unit.unit_conf['acquisition']

        self.latest_acquisition = Acquisition(
            target_ra=target_ra_j2000_hours,
            target_dec=target_dec_j2000_degs,
            conf=acquisition_conf,
        )

        self.unit.start_activity(UnitActivities.Acquiring)
        phase = 'sky'

        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        #
        # move the stage and mount into position
        #
        self.unit.start_activity(UnitActivities.Positioning)
        self.unit.stage.move_to_preset(StagePresetPosition.Sky)

        self.unit.mount.start_tracking()
        self.unit.mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
        while self.unit.stage.is_moving or self.unit.mount.is_slewing:
            time.sleep(1)
        logger.info(f"{op}: sleeping 10 seconds to let the mount stop ...")
        time.sleep(10)
        self.unit.end_activity(UnitActivities.Positioning)

        #
        # set the camera for phase1 of acquisition mode (stage at Sky position)
        #
        # self.folder = PathMaker().make_acquisition_folder(
        #     tags={
        #         'target': f"{target_ra_j2000_hours},{target_dec_j2000_degs}"
        #     })

        sky_settings = CameraSettings(
            seconds=acquisition_conf['exposure'],
            base_folder=os.path.join(self.latest_acquisition.folder, phase),
            gain=acquisition_conf['gain'],
            binning=CameraBinning(acquisition_conf['binning']['x'], acquisition_conf['binning']['y']),
            roi=UnitRoi.from_dict(acquisition_conf['roi']).to_camera_roi(),
            save=True
        )

        #
        # loop trying to solve and correct the mount till within tolerances
        #
        tries: int = acquisition_conf['tries'] if 'tries' in acquisition_conf else 3

        # set up the tolerances
        default_tolerance: Angle = Angle(1 * u.arcsecond)
        ra_tolerance: Angle = default_tolerance
        dec_tolerance: Angle = default_tolerance
        if 'tolerance' in acquisition_conf:
            if 'ra_arcsec' in acquisition_conf['tolerance']:
                ra_tolerance = Angle(acquisition_conf['tolerance']['ra_arcsec'] * u.arcsecond)
            if 'dec_arcsec' in acquisition_conf['tolerance']:
                dec_tolerance = Angle(acquisition_conf['tolerance']['dec_arcsec'] * u.arcsecond)

        target = Coord(ra=Angle(target_ra_j2000_hours * u.hour), dec=Angle(target_dec_j2000_degs * u.deg))

        if not self.unit.solver.solve_and_correct(target=target,
                                                  camera_settings=sky_settings,
                                                  solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                  parent_activity=UnitActivities.Acquiring,
                                                  phase='sky',
                                                  max_tries=tries):
            logger.info(f"{op}: {phase=} failed")
            self.latest_acquisition.save_corrections(phase)
            self.unit.end_activity(UnitActivities.Acquiring)
            return

        self.latest_acquisition.save_corrections(phase)

        #
        # we managed to get within tolerances
        #
        phase = 'spec'
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")

        self.unit.stage.move_to_preset(StagePresetPosition.Spec)
        while self.unit.stage.is_moving:
            time.sleep(.2)

        # guiding_conf = self.unit.unit_conf['guiding']
        # binning: CameraBinning = CameraBinning(guiding_conf['binning'], guiding_conf['binning'])
        #
        # unit_roi = UnitRoi.from_dict(guiding_conf['roi'])  # we use only the center and compute the sizes
        # half_width = min(unit_roi.fiber_x, self.unit.camera.cameraXSize - unit_roi.fiber_x) - 300
        # unit_roi.width = half_width * 2
        # half_height = min(unit_roi.fiber_y, self.unit.camera.cameraYSize - unit_roi.fiber_y) - 200
        # unit_roi.height = half_height * 2

        # spec_settings = CameraSettings(
        #     seconds=guiding_conf['exposure'],
        #     binning=binning,
        #     roi=unit_roi.to_camera_roi(binning=binning),
        #     gain=guiding_conf['gain'] if 'gain' in guiding_conf else None,
        #     base_folder=os.path.join(self.latest_acquisition.folder, 'spec'),
        #     save=True
        # )

        spec_settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(self.latest_acquisition.folder, phase))
        success = self.unit.solver.solve_and_correct(target=target,
                                                     camera_settings=spec_settings,
                                                     solving_tolerance=SolvingTolerance(ra_tolerance, dec_tolerance),
                                                     phase=phase,
                                                     parent_activity=UnitActivities.Acquiring,
                                                     max_tries=tries)
        logger.info(f"{op}: {phase=} " + 'succeeded' if success else 'failed')
        if success:
            self.unit.reference_image = self.unit.camera.image
        self.latest_acquisition.save_corrections(phase)
        self.unit.end_activity(UnitActivities.Acquiring)

        phase = 'guiding'
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        logger.info(f"{op}: >>> starting {phase=} <<<")
        logger.info(f"{op}: >>>>>>>>>>>>>>>>>>>>>>>>>>")
        self.unit.guider.do_guide_by_solving_with_shm(
            target=target,
            folder=os.path.join(self.latest_acquisition.folder, phase)
        )

        self.unit.acquirer.latest_acquisition.post_process()

    def start_acquisition(self, ra_j2000_hours: float, dec_j2000_degs: float):
        Thread(name='acquisition', target=self.do_acquire, args=[ra_j2000_hours, dec_j2000_degs]).start()

    def start_one_solve_and_correct(self, ra_j2000_hours: float, dec_j2000_degs: float):
        self.latest_acquisition = Acquisition(target_ra=ra_j2000_hours,
                                              target_dec=dec_j2000_degs,
                                              conf=self.unit.unit_conf['acquisition'])
        Thread(
            name='solve-and-correct',
            target=self.do_solve_and_correct,
            args=[ra_j2000_hours, dec_j2000_degs, 'testing']).start()

    # def save_and_plot_corrections(self, folder: str):
    #     path = os.path.join(folder, 'corrections.json')
    #     with open(path, 'w') as fp:
    #         json.dump(self.unit.corrections, fp, indent=2)
    #     plot_corrections_sequence(path)
    #     Filer().move_ram_to_shared(path)
