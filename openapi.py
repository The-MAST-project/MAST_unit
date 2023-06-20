import inspect
from fastapi.openapi.utils import get_openapi

import stage
from mastapi import Mastapi
from docstring_parser import parse, DocstringStyle
from utils import Subsystem
from typing import Union
from powered_device import Socket, SocketId


class TypeToSchema:
    t: type
    schema: dict

    def __init__(self, t: type, schema: dict):
        self.t = t
        self.schema = schema


def make_parameters(method_name, method, docstring) -> list:

    types_to_schemas = [
        TypeToSchema(int, {'type': 'integer', 'format': 'int32'}),
        TypeToSchema(float, {'type': 'number', 'format': 'float'}),
        TypeToSchema(str, {'type': 'string'}),
        TypeToSchema(Union[int, str], {'type': 'number', 'format': 'int32'}),
        TypeToSchema(Union[float, str], {'type': 'number', 'format': 'float'}),
        TypeToSchema(Union[stage.StageState, str], {'type': 'string', 'enum': ['Guiding', 'Science']}),
        TypeToSchema(Union[SocketId, str], {'type': 'string', 'enum': Socket.names()}),
    ]

    parameters_list = list()
    annotations = inspect.get_annotations(method)

    for param_id, param_name in enumerate(annotations.keys()):
        if param_name == 'return':
            continue
        param_dict = {
            'name': param_name,
            'in': 'query',
            'required': True,
        }
        if docstring and docstring.params and param_id < len(docstring.params):
            param_dict['description'] = docstring.params[param_id].description
        param_type = annotations[param_name]

        found = [x for x in types_to_schemas if x.t == param_type]
        if len(found) == 0:
            print(f'make_parameters: MISSING method: {method_name}, parameter type {param_type} for param: {param_name}')
        param_dict['schema'] = found[0].schema if not len(found) == 0 else None
        parameters_list.append(param_dict)

    return parameters_list


def make_openapi_schema(app, subsystems: list[Subsystem]):

    openapi_schema = get_openapi(
        title="Welcome to the mistery show!",
        version="1.0",
        description="This page allows you to explore the MAST Api",
        routes=app.routes,
    )

    openapi_schema['servers'] = [{
        'url': 'http://127.0.0.1:8000/mast/api/v1'
    }]

    openapi_schema['paths'] = dict()
    for sub in subsystems:
        tuples = inspect.getmembers(sub.obj, inspect.ismethod)
        for tup in tuples:
            method_name = tup[0]
            method = tup[1]
            path = f'/{sub.path}/{method_name}'
            if (sub.path == 'planewave' and method_name == 'status' or
                    method_name.startswith('mount_') or
                    method_name.startswith('focuser_') or
                    method_name.startswith('virtualcamera_')) or \
                    Mastapi.is_api_method(method):
                docstring = parse(method.__doc__.replace(':mastapi:\n', ''), style=DocstringStyle.NUMPYDOC) \
                    if method.__doc__ else None
                description = None
                returns = None
                raises = None
                parameters = None
                if docstring:
                    description = docstring.short_description if docstring.short_description is not None else None
                    returns = docstring.returns.description if docstring.returns is not None else None
                    if len(docstring.raises) > 0:
                        raises = docstring.raises[0].description
                    parameters = make_parameters(method_name, method, docstring)
            else:
                continue

            openapi_schema['paths'][path] = {
                'get': {
                    'tags': [sub.path],
                    'description': description,
                    'raises': raises,
                    'returns': returns,
                    'parameters': parameters,
                    'responses': {
                        '200': {
                            'description': 'OK'
                        }
                    }
                }
            }

    app.openapi_schema = openapi_schema
    return app.openapi_schema