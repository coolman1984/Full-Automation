"""Validate MCP argument shapes and literal workflow inputs."""
import math
import re

from .common import AutomationError


def check_schema(value,schema,path="arguments", *, templates=False):
    if templates and isinstance(value, str) and re.fullmatch(r"\$\{[^{}]+\}", value):
        return
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                check_schema(value,option,path,templates=templates)
                return
            except AutomationError:
                pass
        raise AutomationError(path+" does not match an accepted type.")
    types={"object":dict,"array":list,"string":str,"number":(int,float),"integer":int,"boolean":bool}
    kind=schema.get("type")
    if kind and (not isinstance(value,types[kind]) or kind in {"number","integer"} and isinstance(value,bool)):
        raise AutomationError(path+" must be "+kind)
    if kind in {"number","integer"} and isinstance(value,float) and not math.isfinite(value):
        raise AutomationError(path+" must be finite")
    if kind=="object":
        for key in schema.get("required",[]):
            if key not in value:
                raise AutomationError(path+" missing "+key)
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False:
            unknown=set(value)-set(props)
            if unknown:
                raise AutomationError("Unknown argument(s): "+", ".join(sorted(unknown)))
        for key,child in value.items():
            if key in props:
                check_schema(child,props[key],path+"."+key,templates=templates)
    if kind=="array" and "items" in schema:
        for child in value:
            check_schema(child,schema["items"],path+"[]",templates=templates)
