import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PlaneWave import pwi4_client
from common.utils import init_log
import logging
from contextlib import asynccontextmanager
import psutil
import os
from fastapi.responses import RedirectResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from common.process import ensure_process_is_running
from common.config import Config

#
# Log level configuration from the 'global' section of the 'config' file
#
global_conf = Config().toml['global']
if 'log_level' in global_conf:
    log_level = getattr(logging, global_conf['log_level'].upper())
else:
    log_level = logging.WARNING
logging.basicConfig(level=log_level)
logger = logging.getLogger('mast.unit')
init_log(logger, level=log_level)

logger.info('Starting ...')

pw = None


def app_quit():
    logger.info('Quiting!')
    parent_pid = os.getpid()
    parent = psutil.Process(parent_pid)
    for child in parent.children(recursive=True):  # or parent.children() for recursive=False
        child.kill()
    parent.kill()


ensure_process_is_running(pattern='PWI4',
                          cmd='C:\\Program Files (x86)\\PlaneWave Instruments\\PlaneWave Interface 4\\PWI4.exe',
                          logger=logger, shell=True)
ensure_process_is_running(pattern='PWShutter',
                          cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\" +
                              "PlaneWave Shutter Control\\PWShutter.exe",
                          logger=logger,
                          shell=True)

from camera import router as camera_router
from covers import router as covers_router
from mount import router as mount_router
from focuser import router as focuser_router
from unit import router as unit_router
from unit import unit

while True:
    try:
        pw = pwi4_client.PWI4()
        pw.status()
        logger.info(f"Connected to PWI4")
        break
    except pwi4_client.PWException as ex:
        logger.info(f"no PWI4 yet ...")
        continue
    except Exception as ex:
        logger.error("cannot connect to PWI4", exc_info=ex)
        app_quit()

# unit = Unit(unit_id)
# if not unit:
#     logger.error("cannot create a Unit")
#     app_quit()


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    unit.start_lifespan()
    yield
    unit.end_lifespan()


app = FastAPI(
    docs_url='/docs',
    redocs_url=None,
    lifespan=lifespan,
    openapi_url='/openapi.json',
    debug=True,
    default_response_class=ORJSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(camera_router)
app.include_router(covers_router)
app.include_router(mount_router)
app.include_router(focuser_router)
app.include_router(unit_router)


@app.get("/favicon.ico")
def read_favicon():
    return RedirectResponse(url="/static/favicon.ico")


# subsystems = [
#     Subsystem(path='unit', obj=unit, obj_name='unit'),
#     Subsystem(path='mount', obj=unit.mount, obj_name='unit.mount'),
#     Subsystem(path='focuser', obj=unit.focuser, obj_name='unit.focuser'),
#     # Subsystem(path='camera', obj=unit.camera, obj_name='unit.camera'),
#     Subsystem(path='stage', obj=unit.stage, obj_name='unit.stage'),
#     Subsystem(path='covers', obj=unit.covers, obj_name='unit.covers'),
#     Subsystem(path='planewave', obj=pw, obj_name='pw')
# ]


# def get_api_methods(subs):
#     """
#     Preliminary inspection of the API methods.
#
#     Per each defined subsystem:
#     - For all methods tagged as Mastapi.is_api_method remember
#         - The method name (used for calling it later)
#         - The method object
#         - The method object's __doc__
#
#     Parameters
#     ----------
#     subs
#
#     Returns
#     -------
#
#     """
#     for sub in subs:
#         method_tuples = inspect.getmembers(sub.obj, inspect.ismethod)
#         api_method_tuples = [t for t in method_tuples if Mastapi.is_api_method(t[1]) or
#                              (sub.path == 'planewave' and not t[0].startswith('_'))]
#         sub.method_names = [t[0] for t in api_method_tuples]
#         sub.method_objects = [t[1] for t in api_method_tuples]
#         for o in sub.method_objects:
#             sub.method_docs = [o.__doc__.replace(':mastapi:\n', '').lstrip('\n').strip() if o.__doc__ else None]
#
#
# make_openapi_schema(app=app, subsystems=subsystems)
# get_api_methods(subs=subsystems)
#
#
# @app.get(BASE_UNIT_PATH + '/{subsystem}/{method}')
# async def do_item(subsystem: str, method: str, request: Request):
#
#     sub = [s for s in subsystems if s.path == subsystem]
#     if len(sub) == 0:
#         return f'Invalid MAST subsystem \'{subsystem}\', valid ones: {", ".join([x.path for x in subsystems])}'
#
#     sub = sub[0]
#
#     if method == 'quit':
#         app_quit()
#
#     if method == 'help':
#         responses = list()
#         for i, obj in enumerate(sub.method_objects):
#             responses.append(HelpResponse(sub.method_names[i], sub.method_docs[i]))
#         return responses
#
#     if method not in sub.method_names:
#         return CanonicalResponse(errors=f"Invalid method '{method}' for " +
#                                         f"subsystem {subsystem}, valid ones: {", ".join(sub.method_names)}")
#
#     cmd = f'{sub.obj_name}.{method}('
#     for k, v in request.query_params.items():
#         cmd += f"{k}={quote(v)}, "
#     cmd = cmd.removesuffix(', ') + ')'
#
#     try:
#         ret = eval(cmd)
#         ret = CanonicalResponse(value=ret)
#     except Exception as e:
#         ret = CanonicalResponse(exception=e)
#
#     return ret
#
#
# @app.get(BASE_UNIT_PATH + '/{method}')
# def do_unit(method: str, request: Request):
#     sub = [s for s in subsystems if s.obj_name == 'unit']
#     sub = sub[0]
#     if method == 'quit':
#         app_quit()
#
#     if method == 'help':
#         responses = list()
#         for i, obj in enumerate(sub.method_objects):
#             responses.append(HelpResponse(sub.method_names[i], sub.method_docs[i]))
#         return responses
#
#     if method not in sub.method_names:
#         return f'Invalid method \'{method}\' for subsystem {sub.obj_name}, valid ones: {", ".join(sub.method_names)}'
#
#     cmd = f'{sub.obj_name}.{method}('
#     for k, v in request.query_params.items():
#         cmd += f"{k}={quote(v)}, "
#     cmd = cmd.removesuffix(', ') + ')'
#
#     try:
#         ret = eval(cmd)
#         ret = CanonicalResponse(value=ret)
#     except Exception as e:
#         ret = CanonicalResponse(exception=e)
#
#     return ret


if __name__ == "__main__":
    server_conf = Config().toml['server']
    port = server_conf['port'] if 'port' in server_conf else 8000

    logger.info("The MAST Unit server is starting ...")

    uvicorn.run(app, host="0.0.0.0", port=port,log_level=log_level)
