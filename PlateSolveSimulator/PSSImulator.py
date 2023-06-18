#
# Forever:
#  - Try to acquire PlateSolving named semaphore
#  - When acquired, get parameters from PlateSolving_Params shared segment (specifically ra and dec)
#  - Copy ra and dec to Results shared segment
#  - Release PlateSolving Semaphore
#  - Wait a few seconds
#  - Go back to trying to acquire semaphore
#
import os.path
import time

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


def solve_image():
    if 'ra' in image_params_dict.keys() and 'dec' in image_params_dict.keys():
        ra = float(image_params_dict['ra'])
        dec = float(image_params_dict['dec'])
        d = {
            'solved': True,
            'ra': ra,
            'dec': dec,
        }
        store_params(results_shm, d)
        hdu = fits.hdu.PrimaryHDU()
        hdu.header['NumX'] = int(image_params_dict['NumX'])
        hdu.header['NumY'] = int(image_params_dict['NumY'])
        hdu.data = image_shm.buf
        counter = image_counter.value
        hdu.writeto(os.path.join(image_dir, f'image-{counter}.fits'))
        image_counter.value = counter + 1
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

    with open('.pid', 'w') as f:
        f.write(f'{os.getpid()}\n')
    init_log(logger)
    logger.info('---------------')
    logger.info('New PSSimulator')
    logger.info('---------------')

    semaphore = Semaphore('PlateSolving')

    got_memory_segments = False
    got_semaphore = False
    while not (got_memory_segments and got_semaphore):
        try:
            semaphore.open()
            got_semaphore = True
        except AssertionError:
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
            image_param_dict = parse_params(image_params_shm)
            solve_image()
            # let the guiding software know that the results are available
            semaphore.release()
            logger.info(f"semaphore released")
        except Exception as e:
            logger.error('exception: ', e)
