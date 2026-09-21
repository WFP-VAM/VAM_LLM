"""Fresh, single-turn Vertex requests; no tools, chat history or automatic inference retries."""
import hashlib
import json
import time
from datetime import datetime, timezone


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


class VertexProvider:
    def __init__(self, settings):
        self.settings = settings

    def complete(self, request, timeout):
        from google import genai
        from google.genai import types
        if not self.settings.project:
            raise ValueError('Explicit Seasonal project required')
        parts = [types.Part.from_text(text=json.dumps(request['payload'], ensure_ascii=False))]
        for figure in request['images']:
            if not figure['uri'].startswith('gs://'):
                raise ValueError('Vertex images must use original GCS objects')
            parts += [types.Part.from_text(text=json.dumps({k: figure[k] for k in ('figure_id', 'metadata_note')})),
                      types.Part.from_uri(file_uri=figure['uri'], mime_type=figure['mime'])]
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        with genai.Client(vertexai=True, project=self.settings.project, location=self.settings.location,
                          http_options=types.HttpOptions(timeout=timeout * 1000,
                              headers={'X-Vertex-AI-LLM-Request-Type': 'shared'},
                              retry_options=types.HttpRetryOptions(attempts=1))) as client:
            response = client.models.generate_content(model=self.settings.model,
                contents=types.Content(role='user', parts=parts),
                config=types.GenerateContentConfig(system_instruction=request['system'], temperature=1.0,
                    max_output_tokens=32768 if request['images'] else 65536,
                    thinking_config=types.ThinkingConfig(thinking_level='HIGH', include_thoughts=False),
                    media_resolution='MEDIA_RESOLUTION_HIGH', response_mime_type='application/json',
                    response_schema=vertex_schema(request['schema'])))
        raw = response.model_dump(mode='json', exclude_none=True)
        candidates = raw.get('candidates', [])
        candidate = candidates[0] if candidates else {}
        text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', []) if not p.get('thought'))
        return dict(text=text, finish_reason=candidate.get('finish_reason', 'BLOCKED'), raw=raw,
                    diagnostic=dict(model=raw.get('model_version', self.settings.model), requested_model=self.settings.model,
                        location=self.settings.location, started_at=timestamp, duration_seconds=time.monotonic()-started,
                        timeout_seconds=timeout, attempts=1, thinking='HIGH', temperature=1.0,
                        prompt_hash=digest(request['system']), schema_hash=digest(request['schema']),
                        token_usage=raw.get('usage_metadata', {}), response_id=raw.get('response_id')))
