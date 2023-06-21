import os.path
import time
import numpy as np
from semaphore_win_ctypes import Semaphore
from multiprocessing.shared_memory import SharedMemory
from time import sleep
from utils import parse_params, store_params
from astropy.io import fits
import os
import logging

image_params_shm: SharedMemory | None = None
image_shm: SharedMemory | None = None
results_shm: SharedMemory | None = None

image_params_dict: dict

image_dir = 'images'
logger = logging.getLogger('PSSimulator')


class ImageCounter:
    filename: str = os.path.join(image_dir, '.counter')

    @property
    def value(self) -> int:
        try:
            with open(self.filename, 'r') as f:
                ret = int(f.readline())
        except FileNotFoundError:
            ret = 0
        return ret

    @value.setter
    def value(self, v: int):
        with open(self.filename, 'w') as f:
            f.write(f'{v}\n')


image_counter = ImageCounter()


def solve_image(params: dict):
    """
    A simulated plate solver.  It:
    - gets the image parameters (a dictionary) from the guider process
    - saves the image from the 'PlateSolving_Image' shared memory segment into a FITS file (just to check
        the sharing mechanism works)
    - copies the input parameters to the 'PlateSolving_Results' shared memory segment

    Parameters
    ----------
    params: dict - A dictionary previously created from a name=value list in the 'PlateSolving_Params'
                     shared memory segment

    Returns
    -------

    """
    if 'ra' in params.keys() and 'dec' in params.keys():
        ra = float(params['ra'])
        dec = float(params['dec'])
        d = {
            'solved': True,
            'ra': ra,
            'dec': dec,
        }
        store_params(results_shm, d)
        NumX = int(params['NumX'])
        NumY = int(params['NumY'])
        image = np.ndarray((NumX, NumY), dtype=np.uint32, buffer=image_shm.buf)
        header = {
            'NAXIS1': NumY,
            'NAXIS2': NumX,
            'RA': ra,
            'DEC': dec,
        }
        hdu = fits.hdu.PrimaryHDU(image, header=header)

        counter = image_counter.value
        os.makedirs('images', exist_ok=True)
        image_counter.value = counter + 1

        hdu.writeto(os.path.join(image_dir, f'image-{counter}.fits'))
        print(f'solved image: ra={ra} dec={dec}')


def init_log(logger: logging.Logger):
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - {%(name)s:%(threadName)s:%(thread)s} - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    handler = logging.FileHandler(filename='PSSimulator.log', mode='a')
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


if __name__ == '__main__':

    init_log(logger)
    logger.info('---------------')
    logger.info('New PSSimulator')
    logger.info('---------------')

    semaphore = Semaphore('PlateSolving')

    #
    # The shared resources (semaphore and shared memory segments) get created
    #  by the guiding software.  We can only patiently wait for them to get created before we
    #  can use them
    #
    got_memory_segments = False
    got_semaphore = False
    while not (got_memory_segments and got_semaphore):
        try:
            semaphore.open()
            got_semaphore = True
        except (AssertionError, FileNotFoundError):
            pass

        try:
            image_params_shm = SharedMemory(name='PlateSolving_Params')
            image_shm = SharedMemory(name='PlateSolving_Image')
            results_shm = SharedMemory(name='PlateSolving_Results')
            got_memory_segments = True
        except FileNotFoundError:
            pass

        if not (got_semaphore and got_memory_segments):
            logger.info("Waiting for the shared resources ...")
            sleep(5)

    while True:
        """
        Loop forever (or until killed by the guiding process)
        """
        try:
            # wait for the guider software to acquire the image and place it in the shared segment
            semaphore.acquire(timeout_ms=None)
            logger.info(f"semaphore acquired")
            image_param_dict = parse_params(image_params_shm, logger)
            if not image_param_dict:
                semaphore.release()
                continue

            solve_image(image_param_dict)
            # let the guiding software know that the results are available
            semaphore.release()
            logger.info(f"semaphore released")
            time.sleep(1)
        except Exception as e:
            logger.error('exception: ', e)
