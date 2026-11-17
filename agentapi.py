"""What agents see of Voxa: the OpenAPI description, llms.txt, and the MCP server.

One table (OPERATIONS) describes every operation, and the three views are built from it, so they
cannot drift apart. This module knows nothing about HTTP handlers or accounts: the server hands
the MCP dispatcher a `call(name, args)` function, and it raises ApiError for anything it wants
the caller told.

MCP is the Model Context Protocol over its "streamable HTTP" transport: JSON-RPC messages
POSTed to /mcp, each answered with one JSON response (there is no server-sent stream).
"""
import json

NAME = 'Voxa'
VERSION = '1.0.0'
MCP_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')


class ApiError(Exception):
    """An operation that cannot be done. `status` is the HTTP code, `message` is safe to show."""

    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


LANG_NOTE = ('Language code: a American English, b British English, e Spanish, f French, h Hindi, '
             'i Italian, j Japanese, p Brazilian Portuguese, z Mandarin Chinese. Default a.')
VOICE_NOTE = 'A Studio voice id from list_voices for that language. Default: the language\'s default voice.'

SPEECH = {
    'type': 'object', 'required': ['text'],
    'properties': {
        'text': {'type': 'string', 'description': 'What to say, up to 4,000 characters. For longer text use make_audiobook.'},
        'lang': {'type': 'string', 'description': LANG_NOTE},
        'voice': {'type': 'string', 'description': VOICE_NOTE},
        'speed': {'type': 'number', 'description': 'Speaking speed, 0.5 to 2. Default 1.'},
        'format': {'type': 'string', 'enum': ['mp3', 'wav'], 'description': 'Audio format. Default mp3.'},
    }}
AUDIOBOOK = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string', 'description': 'The book\'s title, stored in the MP3.'},
        'text': {'type': 'string', 'description': 'The whole text. Lines like "Chapter 3" or "Part II" start chapters '
                                                 'automatically. Send this or "chapters".'},
        'chapters': {'type': 'array', 'description': 'Explicit chapters, in order. Send this or "text".',
                     'items': {'type': 'object', 'required': ['text'], 'properties': {
                         'title': {'type': 'string'}, 'text': {'type': 'string'}}}},
        'lang': {'type': 'string', 'description': LANG_NOTE},
        'voice': {'type': 'string', 'description': VOICE_NOTE},
    }}
PODCAST = {
    'type': 'object', 'required': ['text'],
    'properties': {
        'text': {'type': 'string', 'description': 'The document to discuss: an article, report, notes, slides as text. '
                                                 'At least a few sentences, up to 200,000 characters.'},
        'focus': {'type': 'string', 'description': 'Optional: what the hosts should concentrate on.'},
        'minutes': {'type': 'integer', 'enum': [5, 10, 20], 'description': 'Episode length. Default 10.'},
        'tone': {'type': 'string', 'enum': ['friendly', 'expert', 'lively', 'critical'], 'description': 'Default friendly.'},
        'lang': {'type': 'string', 'description': LANG_NOTE},
        'title': {'type': 'string', 'description': 'Optional episode title; otherwise the script writer picks one.'},
        'hosts': {'type': 'object', 'description': 'Optional host names: {"a": "Maya", "b": "Leo"}.',
                  'properties': {'a': {'type': 'string'}, 'b': {'type': 'string'}}},
        'voices': {'type': 'object', 'description': 'Optional voice ids per host: {"a": "harper", "b": "adam"}. '
                                                   'Default: a woman\'s and a man\'s voice for the language.',
                   'properties': {'a': {'type': 'string'}, 'b': {'type': 'string'}}},
    }}
JOB_ID = {'type': 'object', 'required': ['id'], 'properties': {
    'id': {'type': 'string', 'description': 'The job id returned when it was started.'}}}

# name -> what it is, who calls it how, and its input schema. `rest` is (method, path).
OPERATIONS = {
    'list_voices': {
        'rest': ('GET', '/api/v1/voices'), 'summary': 'List the Studio voices',
        'description': 'The voices you can pass as "voice", with language, gender and quality class. '
                       'Optionally only one language.',
        'input': {'type': 'object', 'properties': {'lang': {'type': 'string', 'description': LANG_NOTE}}}},
    'speak': {
        'rest': ('POST', '/api/v1/speech'), 'summary': 'Turn text into speech',
        'description': 'Speaks the text and returns the audio. Over REST the response body is the audio file; '
                       'over MCP the result has an audio_url to download. Uses your plan\'s Studio characters.',
        'input': SPEECH},
    'make_audiobook': {
        'rest': ('POST', '/api/v1/audiobooks'), 'summary': 'Start an audiobook',
        'description': 'Reads a whole text into one MP3 with a chapter mark for each chapter. It runs in the '
                       'background: poll get_job with the returned id until status is "done", then download '
                       'audio_url. Characters are taken from your plan when it starts and returned if it fails.',
        'input': AUDIOBOOK},
    'make_podcast': {
        'rest': ('POST', '/api/v1/podcasts'), 'summary': 'Start a two-host podcast episode',
        'description': 'Writes a two-host conversation about the document and records it as one MP3 with chapters. '
                       'Runs in the background: poll get_job until "done". The finished job has the written script '
                       'and the audio_url.',
        'input': PODCAST},
    'get_job': {
        'rest': ('GET', '/api/v1/jobs/{id}'), 'summary': 'Check an audiobook or podcast job',
        'description': 'status is queued, running, done or error. When done: audio_url, duration_seconds and '
                       'chapters (and, for a podcast, the script). Finished files are kept for 24 hours. '
                       'Pass wait_seconds (up to 50) to wait for it to change instead of polling.',
        'input': {'type': 'object', 'required': ['id'], 'properties': {
            'id': JOB_ID['properties']['id'],
            'wait_seconds': {'type': 'integer', 'description': 'Wait up to this many seconds for it to finish. Optional.'}}}},
    'cancel_job': {
        'rest': ('DELETE', '/api/v1/jobs/{id}'), 'summary': 'Cancel a job',
        'description': 'Stops an unfinished audiobook or podcast. Characters already used are not returned.',
        'input': JOB_ID},
    'get_account': {
        'rest': ('GET', '/api/v1/account'), 'summary': 'What this account has left',
        'description': 'The plan, the Studio characters and minutes left, and how many podcasts and audiobooks '
                       'may still be started.',
        'input': {'type': 'object', 'properties': {}}},
}


def openapi(base_url):
    """The OpenAPI 3.1 description of the REST API."""
    paths = {}
    for name, op in OPERATIONS.items():
        method, path = op['rest']
        item = {'operationId': name, 'summary': op['summary'], 'description': op['description'],
                'security': [{'apiKey': []}], 'responses': {
                    '200': {'description': 'OK'},
                    '202': {'description': 'Started; poll the job'},
                    '400': {'description': 'Bad request: the message says what to change'},
                    '401': {'description': 'Missing or invalid API key'},
                    '429': {'description': 'Plan limit reached; the message says when it resets or what to buy'}}}
        schema = op['input']
        if method == 'POST':
            item['requestBody'] = {'required': True, 'content': {'application/json': {'schema': schema}}}
        else:
            item['parameters'] = [{'name': k, 'in': 'path' if '{' + k + '}' in path else 'query',
                                   'required': '{' + k + '}' in path, 'schema': {k2: v for k2, v in v_.items() if k2 != 'description'},
                                   'description': v_.get('description', '')} for k, v_ in schema.get('properties', {}).items()]
        paths.setdefault(path, {})[method.lower()] = item
    paths['/api/v1/files/{name}'] = {'get': {
        'operationId': 'download_file', 'summary': 'Download a finished audio file',
        'description': 'The audio_url of a finished job or speech result. The unguessable name is the secret, so it '
                       'needs no key and can be handed to anyone; files are deleted after 24 hours.',
        'parameters': [{'name': 'name', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
        'responses': {'200': {'description': 'audio/mpeg or audio/wav'}, '404': {'description': 'Unknown or expired'}}}}
    return {
        'openapi': '3.1.0',
        'info': {'title': f'{NAME} API', 'version': VERSION,
                 'description': 'Text to speech, audiobooks and two-host podcasts. Authenticate with an API key '
                                f'made at {base_url}/developers : Authorization: Bearer vx_live_… . Usage comes '
                                'out of the key owner\'s Voxa plan. MCP: ' + base_url + '/mcp'},
        'servers': [{'url': base_url}],
        'components': {'securitySchemes': {'apiKey': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'vx_live_…'}}},
        'paths': paths,
    }


def llms_txt(base_url):
    return f'''# Voxa

> Voxa turns text into natural speech. Agents can speak text, make whole audiobooks (an MP3 with chapter marks) and make two-host podcast episodes from a document, over REST or MCP. Usage comes out of the key owner's Voxa plan.

## Connect
- Get an API key (free account; keys start with vx_live_): {base_url}/developers
- Authenticate every call with `Authorization: Bearer vx_live_…`
- REST: OpenAPI description at {base_url}/api/openapi.json
- MCP (streamable HTTP, JSON-RPC): {base_url}/mcp , with the same Authorization header. Tools: {', '.join(OPERATIONS)}.

## How it works
- `POST /api/v1/speech` (or the `speak` tool): text in, audio out (mp3 or wav). Synchronous.
- `POST /api/v1/audiobooks` (`make_audiobook`): send `text` (chapters start at lines like "Chapter 3") or `chapters: [{{title, text}}]`. Returns a job id with status 202; poll `GET /api/v1/jobs/{{id}}` until `status` is `done`, then download `audio_url`.
- `POST /api/v1/podcasts` (`make_podcast`): send `text` of any document, optional `minutes` (5, 10 or 20), `tone`, `focus`, `hosts`, `voices`. Same job flow; the finished job includes the written `script`.
- `GET /api/v1/voices` lists the voice ids; `GET /api/v1/account` shows what is left.
- Audio files live at unguessable `{base_url}/api/v1/files/<name>.mp3` links for 24 hours; they need no key.

## Limits
- Voices on a device (Voxa Lite's on-device voices) run only in a browser, so the API uses Studio voices, which count against the plan: free accounts have a one-time welcome credit and any packs bought; Studio accounts have a monthly allowance.
- Podcasts: free accounts 1 a week up to 5 minutes; Studio 3 a day up to 20 minutes. Audiobooks: free accounts 1 new book a day; Studio unlimited. An account can have 2 audiobooks or podcasts being made at once. Up to 120 requests a minute per key.
- Errors are JSON `{{"error": "…"}}` with a message that says what to change. 429 means a plan limit; 402-style wallet messages say how much is needed and what to buy.
'''


# --- MCP ---------------------------------------------------------------------------------------

def mcp_tools():
    return [{'name': name, 'description': op['description'], 'inputSchema': op['input']}
            for name, op in OPERATIONS.items()]


def _result(rid, value):
    return {'jsonrpc': '2.0', 'id': rid, 'result': value}


def _error(rid, code, message):
    return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}


def mcp_handle(message, call, authenticated):
    """Answer one JSON-RPC message; None for a notification (nothing to send back).

    `call(name, args)` runs a tool and returns a JSON-able result or raises ApiError.
    `authenticated` says whether the request carried a valid key; it is needed only to call a tool.
    Raises ApiError(401, …) for an unauthenticated tool call, which the server turns into HTTP 401."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _error(None, -32600, 'Invalid request')
    rid, method, params = message.get('id'), message.get('method'), message.get('params') or {}
    if 'method' not in message:
        return None  # a response to something we never asked
    if rid is None:
        return None  # notifications (notifications/initialized, …) get no answer
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return _result(rid, {
            'protocolVersion': asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': NAME, 'version': VERSION},
            'instructions': 'Voxa makes speech, audiobooks and podcasts. Start long work with make_audiobook or '
                            'make_podcast, then call get_job (with wait_seconds) until it is done and give the user '
                            'the audio_url. Use get_account to see what is left.'})
    if method == 'ping':
        return _result(rid, {})
    if method == 'tools/list':
        return _result(rid, {'tools': mcp_tools()})
    if method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        if name not in OPERATIONS:
            return _error(rid, -32602, f'Unknown tool {name!r}')
        if not isinstance(args, dict):
            return _error(rid, -32602, 'arguments must be an object')
        if not authenticated:
            raise ApiError(401, 'Send your Voxa API key as "Authorization: Bearer vx_live_…".')
        try:
            out = call(name, args)
            return _result(rid, {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False, indent=1)}],
                                 'structuredContent': out, 'isError': False})
        except ApiError as e:
            return _result(rid, {'content': [{'type': 'text', 'text': e.message}], 'isError': True})
    return _error(rid, -32601, f'Method not found: {method}')
