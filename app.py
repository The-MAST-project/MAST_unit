import uvicorn
from fastapi import FastAPI, Request
from PlaneWave import pwi4_client
from Unit import Unit

import logging

unit_id = 17

logger = logging.getLogger('mast')
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.info('MAST Server')

app = FastAPI()
pw = pwi4_client.PWI4()
unit = Unit(unit_id)
root = '/mast/api/v1/'


@app.get(root + 'status')
def pw_status(request: Request):
    return pw.status()


@app.get(root + 'unit/{method}')
def do_unit(method: str, request: Request):
    params = request.query_params

    if callable(getattr(unit, method)):
        cmd = f'unit.{method}('
        for k, v in request.query_params.items():
            cmd += f"{k}={v}, "
        cmd = cmd.removesuffix(', ') + ')'
        return eval(cmd)
    else:
        pass


@app.get(root + "{path}/{method}")
def dispatch(path: str, method: str, request: Request):
    params = request.query_params

    pw_methods = [method for method in dir(pw) if callable(getattr(pw, method)) and (
            method.startswith('mount_') or
            method.startswith('focuser_') or
            method.startswith('rotator_') or
            method.startswith('status_') or
            method == 'status'
    )]

    pw_method = [m for m in pw_methods if m.startswith(path + '_' + method)]
    if len(pw_method) == 1:
        cmd = f'pw.{path}_{method}('
        for k, v in request.query_params.items():
            cmd += f"{k}={v}, "
        cmd = cmd.removesuffix(', ') + ')'
        return eval(cmd)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
