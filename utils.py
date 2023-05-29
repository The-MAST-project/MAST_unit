import datetime
import functools
import inspect
import os
import platform
from threading import Timer
from enum import Flag
from starlette.responses import Response
import json
from typing import Any
import logging
import io


class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args,**self.kwargs)


class Activities:
    """
    Tracks start/end of ``MAST`` activities.  Subclassed by ``MAST`` objects
    that have long-running activities.
    """

    activities: Flag

    def start_activity(self, activity: Flag, logger):
        self.activities |= activity
        logger.info(f'activity {activity.name} - started')

    def end_activity(self, activity: Flag, logger):
        self.activities &= ~activity
        logger.info(f'activity {activity.name} - ended')

    def is_active(self, activity: Flag) -> bool:
        return not (self.activities & activity) == 0


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
        utcnow = datetime.datetime.utcnow()
        if utcnow.hour < 12:
            utcnow = utcnow - datetime.timedelta(days=1)
        return os.path.join(top, f'{utcnow:%Y-%m-%d}', self.path)

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
        # self.filename = self.make_file_name()
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        logging.FileHandler.__init__(self, filename='', delay=True, mode=mode, encoding=encoding, errors=errors)


def init_log(logger: logging.Logger):
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    handler = DailyFileHandler(path='app.log', mode='a')
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.info('initialized')


def is_mastapi(func):
    return func.__doc__.contains(':mastapi:')


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

        ret.error = None
        ret.response = None
        try:
            ret.result = func(*args, **kwargs)
        except Exception as ex:
            ret.error = ex
        ret.status = None if status_method is None else status_method()
        return ret

    return wrapper


class HelpResponse:
    method: str
    help: str

    def __init__(self, method: str, doc: str):
        self.method = method
        self.help = doc

