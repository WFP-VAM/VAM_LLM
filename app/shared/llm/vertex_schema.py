"""Vertex transport schemas; application validators retain the complete contracts."""
import json
from copy import deepcopy
from functools import lru_cache
from .errors import ProviderError

class SchemaConfigurationError(ProviderError):
    def __init__(self, message):
        super().__init__(message, kind="configuration")

def check_response_schema(schema):
    import warnings
    from google.genai import types
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        types.Schema.model_validate(deepcopy(schema))

def expand_schema(schema):
    definitions = schema.get("$defs", {})
    def expand(value, trail=()):
        if isinstance(value, list):
            return [expand(item, trail) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            if not ref.startswith("#/$defs/") or ref in trail:
                raise SchemaConfigurationError("Unsupported or recursive schema reference")
            merged = {**definitions[ref.rsplit("/", 1)[-1]], **{k: v for k,v in value.items() if k != "$ref"}}
            return expand(merged, (*trail, ref))
        return {key: expand(item, trail) for key, item in value.items() if key != "$defs"}
    return expand(schema)


def compile_provider_schema(schema):
    """Translate the supported transport subset; retain stricter checks in Pydantic.

    Annotation/default/extra-key/string-length rules are intentionally enforced
    by the application. Unknown structural constructs fail instead of weakening
    the response schema through the SDK's warning-and-drop behavior.
    """
    allowed = {"type", "properties", "items", "required", "enum", "description",
               "nullable", "minimum", "maximum", "minItems", "maxItems", "format"}
    application_only = {"title", "default", "additionalProperties", "minLength", "maxLength"}
    def convert(value):
        unknown = set(value) - allowed - application_only - {"const", "anyOf"}
        if unknown or isinstance(value.get("additionalProperties"), dict):
            raise SchemaConfigurationError(f"Unsupported response schema structure: {sorted(unknown)}")
        if "anyOf" in value:
            choices = [item for item in value["anyOf"] if item.get("type") != "null"]
            if len(choices) != 1 or len(value["anyOf"]) != 2:
                raise SchemaConfigurationError("Response schema requires a concrete type or a nullable concrete type")
            return {**convert(choices[0]), "nullable": True}
        unknown = set(value) - allowed - application_only - {"const"}
        if unknown:
            raise SchemaConfigurationError(f"Unsupported response schema keywords: {sorted(unknown)}")
        result = {key: deepcopy(item) for key, item in value.items() if key in allowed}
        if "const" in value:
            if isinstance(value["const"], str):
                result["enum"] = [value["const"]]
            else:
                result["description"] = f"Must equal {value['const']!r}; checked by application validation."
        if "properties" in result:
            result["properties"] = {key: convert(item) for key, item in result["properties"].items()}
            if len(result["properties"]) > 1:
                # Without an explicit order Vertex generates properties alphabetically, as these contracts
                # always have (notes before sections); the SDK would otherwise send the declaration order.
                result["propertyOrdering"] = sorted(result["properties"])
        if "items" in result:
            result["items"] = convert(result["items"])
        if result.get("type") == "object" and not result.get("properties"):
            raise SchemaConfigurationError("Model-facing objects must have explicit properties")
        return result
    result = convert(schema)
    try:
        check_response_schema(result)
    except Exception as exc:
        raise SchemaConfigurationError("Installed Vertex SDK cannot encode the response contract") from exc
    return result


def seasonal_schema(schema):
    """Inline Pydantic references and retain the documented Gemini schema subset.

    Strict constraints omitted here remain enforced by the original local model.
    """
    definitions = schema.get('$defs', {})
    allowed = {'type', 'description', 'enum', 'format', 'items', 'minItems', 'maxItems',
               'minimum', 'maximum', 'properties', 'required', 'anyOf', 'nullable', 'propertyOrdering'}

    def walk(node, trail=()):
        if isinstance(node, list):
            return [walk(x, trail) for x in node]
        if not isinstance(node, dict):
            return node
        if '$ref' in node:
            ref = node['$ref']
            if not ref.startswith('#/$defs/') or ref in trail:
                raise ValueError('Unsupported or recursive schema reference')
            node = {**definitions[ref.rsplit('/', 1)[-1]], **{k: v for k, v in node.items() if k != '$ref'}}
            trail = (*trail, ref)
        unknown = set(node) - allowed - {'const', '$defs', 'title', 'default', 'additionalProperties', 'minLength', 'maxLength'}
        if unknown:
            raise SchemaConfigurationError(f'Unsupported response schema keywords: {sorted(unknown)}')
        if isinstance(node.get('additionalProperties'), dict):
            raise SchemaConfigurationError('Dynamic object schemas are unsupported')
        node = dict(node)
        if 'const' in node:
            node['enum'] = [node.pop('const')]
        result = {k: ({name: walk(v, trail) for name, v in value.items()} if k == 'properties' else walk(value, trail))
                  for k, value in node.items() if k in allowed}
        if 'properties' in result:
            result['propertyOrdering'] = list(result['properties'])
        return result
    return walk(schema)



@lru_cache(maxsize=256)
def _compile(serialized, model, policy, compiler_version):
    schema = json.loads(serialized)
    if policy not in {"mfi", "declaration"}:
        raise SchemaConfigurationError(f"Unknown Vertex schema conversion policy: {policy}")
    try:
        result = compile_provider_schema(expand_schema(schema)) if policy == "mfi" else seasonal_schema(schema)
        check_response_schema(result)
        return result
    except SchemaConfigurationError:
        raise
    except Exception as exc:
        raise SchemaConfigurationError("Vertex cannot encode the response contract") from exc

def compile_schema(schema, profile):
    if schema is None:
        return None
    # Do not sort here: Seasonal intentionally preserves declaration order.
    serialized = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return deepcopy(_compile(serialized, profile.model, profile.schema_policy, 1))


def wire_schema(schema, profile):
    """Vertex's JSON Schema representation after SDK nullable/type normalization.

    Keep the public SDK model as the serialization boundary. Wire tests verify this projection
    against real generateContent HTTP bodies, including Seasonal's optional fields.
    """
    from google.genai import types
    compiled = compile_schema(schema, profile)
    if compiled is None:
        return None

    def normalize(node):
        if isinstance(node, list):
            return [normalize(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: normalize(value) for key, value in node.items() if key != "properties"}
        if "properties" in node:
            result["properties"] = {key: normalize(value) for key, value in node["properties"].items()}
        # Apply this to the source choices: a stand-alone null becomes nullable during recursion.
        choices = node.get("anyOf")
        if choices and any(choice.get("type") == "null" for choice in choices):
            remaining = [normalize(choice) for choice in choices if choice.get("type") != "null"]
            result["nullable"] = True
            if len(remaining) == 1:
                result.pop("anyOf", None)
                result.update(remaining[0])
            else:
                result["anyOf"] = remaining
        if result.get("type") == "null":
            result.pop("type")
            result["nullable"] = True
        return result

    return types.Schema.model_validate(normalize(compiled)).model_dump(mode="json", exclude_none=True)
