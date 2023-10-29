import datetime
import functools
import inspect
import os
import platform
import socket
from threading import Timer, Lock
from enum import Flag
from starlette.responses import Response
import json
from typing import Any
import logging
import io
import re
import psutil
import subprocess
import time
from multiprocessing import shared_memory
import tomlkit
from tomlkit import TOMLDocument
from astropy.io import fits

default_log_level = logging.DEBUG


class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class Activities:
    """
    Tracks start/end of ``MAST`` activities.  Subclassed by ``MAST`` objects
    that have long-running activities.
    """

    activities: Flag
    activity_start_times: dict = {}

    def start_activity(self, activity: Flag, logger):
        self.activity_start_times[activity] = datetime.datetime.now()
        self.activities |= activity
        logger.info(f'activity {activity.name} - started')

    def end_activity(self, activity: Flag, logger):
        duration = datetime.datetime.now() - self.activity_start_times[activity]
        self.activity_start_times[activity] = None
        self.activities &= ~activity
        logger.info(f'activity {activity.name} - ended (duration: {duration})')

    def is_active(self, activity: Flag) -> bool:
        return activity in self.activities


class AscomDriverInfo:
    """
    Gathers information of the ASCOM driver used by the current class
    """
    name: str
    description: str
    version: str

    def __init__(self, driver):
        if driver is None:
            return
        self.name = driver.Name
        self.description = driver.Description
        self.version = driver.DriverVersion


class PrettyJSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=4,
            separators=(", ", ": "),
        ).encode("utf-8")


class DailyFileHandler(logging.FileHandler):

    filename: str = ''
    path: str

    def make_file_name(self):
        """
        Produces file names for the DailyFileHandler, which rotates them daily at noon (UT).
        The filename has the format <top><daily><bottom> and includes:
        * A top section (either /var/log/mast on Linux or %LOCALAPPDATA%/mast on Windows
        * The daily section (current date as %Y-%m-%d)
        * The bottom path, supplied by the user
        Examples:
        * /var/log/mast/2022-02-17/server/app.log
        * c:\\User\\User\\LocalAppData\\mast\\2022-02-17\\main.log
        :return:
        """
        top = ''
        if platform.platform() == 'Linux':
            top = '/var/log/mast'
        elif platform.platform().startswith('Windows'):
            top = os.path.join(os.path.expandvars('%LOCALAPPDATA%'), 'mast')
        now = datetime.datetime.now()
        if now.hour < 12:
            now = now - datetime.timedelta(days=1)
        return os.path.join(top, f'{now:%Y-%m-%d}', self.path)

    def emit(self, record: logging.LogRecord):
        """
        Overrides the logging.FileHandler's emit method.  It is called every time a log record is to be emitted.
        This function checks whether the handler's filename includes the current date segment.
        If not:
        * A new file name is produced
        * The handler's stream is closed
        * A new stream is opened for the new file
        The record is emitted.
        :param record:
        :return:
        """
        filename = self.make_file_name()
        if not filename == self.filename:
            if self.stream is not None:
                # we have an open file handle, clean it up
                self.stream.flush()
                self.stream.close()
                self.stream = None  # See Issue #21742: _open () might fail.

            self.baseFilename = filename
            os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
            self.stream = self._open()
        logging.StreamHandler.emit(self, record=record)

    def __init__(self, path: str, mode='a', encoding=None, delay=False, errors=None):
        self.path = path
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        logging.FileHandler.__init__(self, filename='', delay=True, mode=mode, encoding=encoding, errors=errors)


def init_log(logger: logging.Logger):
    logger.propagate = False
    logger.setLevel(default_log_level)
    handler = logging.StreamHandler()
    handler.setLevel(default_log_level)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - {%(name)s:%(funcName)s:%(threadName)s:%(thread)s}' +
                                  ' -  %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    path_maker = SingletonFactory.get_instance(PathMaker)
    handler = DailyFileHandler(path=os.path.join(path_maker.make_daily_folder_name(), 'log.txt'), mode='a')
    handler.setLevel(default_log_level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def is_mastapi(func):
    return func.__doc__.contains(':mastapi:')


def quote(s: str):
    # return 'abc'
    return "'" + s.replace("'", "\\'") + "'"


class ResultWithStatus:
    """
    Encapsulates the result of a ``MAST`` API call
    """
    result: Any
    error: Any
    status: Any


def return_with_status(func):
    """
    A decorator for ``MAST`` object methods.  A function thus decorated will return an object containing:
    * result: The function's output
    * error: Any exception that may have been raised
    * status: The product of this class' status() method
    :param func:
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> ResultWithStatus:
        ret = ResultWithStatus()

        # find out the current object's status() method
        obj = args[0]
        status_method = None
        for tup in inspect.getmembers(obj, inspect.ismethod):
            if tup[0] == 'status':
                status_method = tup[1]
                break

        ret.result = None
        ret.error = None
        try:
            ret.result = func(*args, **kwargs)
        except Exception as ex:
            ret.error = ex
        ret.status = None if status_method is None else status_method()
        return ret

    return wrapper


class HelpResponse:
    method: str
    description: str

    def __init__(self, method: str, doc: str):
        self.method = method
        self.description = doc


class TimeStamped:
    timestamp: datetime

    def stamp(self):
        self.timestamp = datetime.datetime.now()


class Subsystem:
    path: str
    obj: object
    obj_name: str
    method_objects: list[object]
    method_names: list[str]
    method_docs: list[str]

    def __init__(self, path: str, obj: object, obj_name: str):
        self.path = path
        self.obj = obj
        self.obj_name = obj_name


def parse_params(memory: shared_memory.SharedMemory, logger: logging.Logger) -> dict:
    bytes_array = bytearray(memory.buf)
    string_array = bytes_array.decode(encoding='utf-8')
    data = string_array[:string_array.find('\x00')]
    logger.info(f"data: '{data}'")

    matches = re.findall(r'(\w+(?:\(\d+\))?)\s*=\s*(.*?)(?=(!|$|\w+(\(\d+\))?\s*=))', data)
    d = {}
    for match in matches:
        key = match[0]
        value = match[1].strip()
        logger.info(f"key={match[0]}, value='{value}'")
        d[key] = value
    return d


def store_params(memory: shared_memory.SharedMemory, d: dict):
    params = []
    for k, v in d.items():
        params.append(f'{k}={v}')
    data = ' '.join(params)
    memory.buf[:memory.size] = bytearray(memory.size)  # wipe it clean
    memory.buf[:len(data)] = bytearray(data.encode(encoding='utf-8'))


def find_process(patt: str = None, pid: int | None = None) -> psutil.Process:
    """
    Searches for a running process either by a pattern in the command line or by pid

    Parameters
    ----------
    patt
    pid

    Returns
    -------

    """
    ret = None
    if patt:
        patt = re.compile(patt, re.IGNORECASE)
        for proc in psutil.process_iter():
            try:
                argv = proc.cmdline()
                for arg in argv:
                    if patt.search(arg) and proc.status() == psutil.STATUS_RUNNING:
                        ret = proc
                        break
            except psutil.AccessDenied:
                continue
    elif pid:
        proc = [(x.pid == pid and x.status() == psutil.STATUS_RUNNING) for x in psutil.process_iter()]
        ret = proc[0]

    return ret


def ensure_process_is_running(pattern: str, cmd: str, logger: logging.Logger, env: dict = None,
                              cwd: str = None, shell: bool = False) -> psutil.Process:
    """
    Makes sure a process containing 'pattern' in the command line exists.
    If it's not running, it starts one using 'cmd' and waits till it is running

    Parameters
    ----------
    pattern: str The pattern to lookup in the command line of processes
    cmd: str - The command to use to start a new process
    env: dict - An environment dictionary
    cwd: str - Current working directory
    shell: bool - Run the cmd in a shell
    logger

    Returns
    -------

    """
    p = find_process(pattern)
    if p is not None:
        logger.debug(f'A process with pattern={pattern} in the commandline exists, pid={p.pid}')
        return p

    # It's not running, start it
    if shell:
        process = subprocess.Popen(args=cmd, env=env, shell=True, cwd=cwd)
    else:
        args = cmd.split()
        process = subprocess.Popen(args, env=env, executable=args[0], cwd=cwd)
    logger.info(f"started process (pid={process.pid}) with cmd: '{cmd}'")

    p = None
    while not p:
        p = find_process(pattern)
        if p:
            return p
        logger.info(f"waiting for proces with pattern='{pattern}' to run")
        time.sleep(1)


class SingletonFactory:
    _instances = {}
    _lock = Lock()

    @staticmethod
    def get_instance(class_type):
        with SingletonFactory._lock:
            if class_type not in SingletonFactory._instances:
                SingletonFactory._instances[class_type] = class_type()
        return SingletonFactory._instances[class_type]


config_defaults = """
    [global]
        TopFolder = "C:/MAST"
    
    [stage]
        SpectraPosition = 100000
        ImagePosition = 10000
"""


class ConfigTier:
    """
    Configuration tier.

    We use TOML files and TOMLDocuments to manage our configuration.
    The package we use is tomlkit (https://tomlkit.readthedocs.io/en/latest/) because:
    - TOML is a more rigorously defined .ini format
    - tomlkit supports keeping the order of the lines in the files AND comments.

    The actual configuration object (see Config below) uses three tiers which get merged into one configuration
    """
    mtime: float = None
    file: str | None = None
    defaults: TOMLDocument
    data: TOMLDocument

    def __init__(self, defaults: TOMLDocument = None, file=None):
        """

        Parameters
        ----------
        defaults
        file
        """
        self.file = file
        self.data = tomlkit.TOMLDocument()
        self.defaults = TOMLDocument()
        if defaults:
            self.defaults = defaults
            self.data = self.defaults

        if self.file and os.path.exists(self.file):
            self.load_file()
            self.mtime = os.path.getmtime(self.file)

    def load_file(self):
        if os.path.exists(self.file):
            with open(self.file, 'r') as f:
                file_values = tomlkit.load(f)
                self.data.clear()
                self.data.update(self.defaults)
                self.data.update(file_values)
                self.mtime = os.path.getmtime(self.file)

    def check_and_reload(self):
        current_mtime = os.path.getmtime(self.file) if os.path.exists(self.file) else None
        if current_mtime != self.mtime:
            self.load_file()


class Config:
    """
    Multi-tiered configuration for the MAST system.  It is based on a hierarchy ConfigTiers (see above)

    The tiers are merged in the following order:
    - first some hardcoded default values
    - next, global values loaded from the TopDir/config/mast.ini TOML file (if existent)
    - last (highest priority) host-specific values loaded from the TopDir/config/<hostname>.ini file (if existent)

    The configuration can be saved, the saved values go into the host-specific file.
    """
    data: TOMLDocument

    def __init__(self):
        self.default_config: ConfigTier = ConfigTier(defaults=tomlkit.parse(config_defaults))
        main_config_file = os.path.join('C:\\', 'MAST', 'config', 'mast.ini')  # cannot change
        self.global_config: ConfigTier = ConfigTier(file=main_config_file)

        top_folder = self.global_config.data['global']['TopFolder'] or os.path.join('C:\\', 'MAST')
        self.host_config: ConfigTier = ConfigTier(file=os.path.join(top_folder, 'config', socket.gethostname()))

        self.data = TOMLDocument()
        self.reload()

    def reload(self):
        self.data.clear()
        self.data.update(self.default_config.data)
        for tier in self.global_config, self.host_config:
            tier.check_and_reload()
            self.data.update(tier.data)

    def get(self, section: str, item: str):
        self.reload()
        if section in self.data:
            if item in self.data[section]:
                return self.data[section][item]
            else:
                raise KeyError(f"No item '{item} in section '{section}' in the configuration")
        else:
            raise KeyError(f"No section '{section} in the configuration")

    def set(self, section: str, item: str, value, comment=None):
        """
        Configuration changes are saved in the host-configuration tier

        Parameters
        ----------
        section
           The configuration section
        item
           The configuration item withing the specified section
        value
           The item's value
        comment
           Optional comment

        Returns
        -------

        """
        self.host_config.data[section][item] = value
        if comment:
            self.host_config.data[section][item].comment(comment)

    def save(self):
        """
        TBD
        Returns
        -------

        """
        with open(self.host_config.file, 'w') as f:
            tomlkit.dump(self.host_config.data, f)


config = SingletonFactory.get_instance(Config)


class PathMaker:
    top_folder: str

    def __init__(self):
        self.top_folder = config.get('global', 'TopFolder')
        pass

    @staticmethod
    def make_seq(path: str):
        seq_file = os.path.join(path, '.seq')

        os.makedirs(os.path.dirname(seq_file), exist_ok=True)
        if os.path.exists(seq_file):
            with open(seq_file) as f:
                seq = int(f.readline())
        else:
            seq = 0
        seq += 1
        with open(seq_file, 'w') as file:
            file.write(f'{seq}\n')

        return seq

    def make_daily_folder_name(self):
        dir = os.path.join(self.top_folder, datetime.datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(dir, exist_ok=True)
        return dir

    def make_exposure_file_name(self):
        exposures_folder = os.path.join(self.make_daily_folder_name(), 'Exposures')
        os.makedirs(exposures_folder, exist_ok=True)
        return os.path.join(exposures_folder, f'exposure-{path_maker.make_seq(exposures_folder):04d}')

    def make_acquisition_folder_name(self):
        acquisitions_folder = os.path.join(self.make_daily_folder_name(), 'Acquisitions')
        os.makedirs(acquisitions_folder, exist_ok=True)
        return os.path.join(acquisitions_folder, f'acquisition-{PathMaker.make_seq(acquisitions_folder)}')

    def make_guiding_folder_name(self):
        guiding_folder = os.path.join(self.make_daily_folder_name(), 'Guidings')
        os.makedirs(guiding_folder, exist_ok=True)
        return os.path.join(guiding_folder, f'guiding-{PathMaker.make_seq(guiding_folder)}')

    def make_logfile_name(self):
        daily_folder = os.path.join(self.make_daily_folder_name())
        os.makedirs(daily_folder)
        return os.path.join(daily_folder, 'log.txt')


# A path-maker singleton
path_maker = SingletonFactory.get_instance(PathMaker)


def image_to_fits(image, path: str, header: dict):
    """

    Parameters
    ----------
    image
        an ASCOM ImageArray
    path
        name of the created file
    header
        a dictionary of FITS header key/values

    Returns
    -------

    """
    if not path:
        raise 'Must supply a path to the file'
    if not path.endswith('.fits'):
        path += '.fits'

    hdu = fits.PrimaryHDU(image)
    for k, v in header.items():
        hdu.header[k] = v
    hdul = fits.HDUList([hdu])
    hdul.writeto(path)