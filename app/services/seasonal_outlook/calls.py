"""Seasonal's model calls through the shared LLM client: the model profile, each stage's request and its schema.

Every stage is one fresh, single-turn request with no tools or chat history. The client retries a transient
failure once, and the analysis record keeps every attempt's request and response.
"""
import hashlib
import json

from app.shared.llm import FilePart, LLMRequest, ModelProfile

THINKING_LEVEL = 'HIGH'
TEMPERATURE = 1.0


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def vertex_schema(schema):
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
        node = dict(node)
        if 'const' in node:
            node['enum'] = [node.pop('const')]
        result = {k: ({name: walk(v, trail) for name, v in value.items()} if k == 'properties' else walk(value, trail))
                  for k, value in node.items() if k in allowed}
        if 'properties' in result:
            result['propertyOrdering'] = list(result['properties'])
        return result
    return walk(schema)


def profile(settings, timeout):
    """The model settings of one phase. The project is always explicit, never the workstation's default."""
    if not settings.project:
        raise ValueError('Explicit Seasonal project required')
    return ModelProfile(service='seasonal-outlook', model=settings.model, location=settings.location,
                        project=settings.project, temperature=TEMPERATURE, timeout_seconds=timeout,
                        thinking_level=THINKING_LEVEL, include_thoughts=False, media_resolution='MEDIA_RESOLUTION_HIGH',
                        attempts=2, headers=(('X-Vertex-AI-LLM-Request-Type', 'shared'),))


def llm_request(request, timeout, work_item):
    """A stage's request as the client sends it: the payload, then each map's note and its original GCS image."""
    parts = [json.dumps(request['payload'], ensure_ascii=False)]
    for figure in request['images']:
        if not figure['uri'].startswith('gs://'):
            raise ValueError('Vertex images must use original GCS objects')
        parts += [json.dumps({k: figure[k] for k in ('figure_id', 'metadata_note')}),
                  FilePart(uri=figure['uri'], mime_type=figure['mime'])]
    return LLMRequest(operation=f"seasonal_outlook.{request['stage']}.v1", node=request['stage'], system=request['system'],
                      parts=parts, response_schema=vertex_schema(request['schema']), json_output=True,
                      max_output_tokens=32768 if request['images'] else 65536, timeout_seconds=timeout,
                      fail_on_truncation=True, work_item=work_item)
